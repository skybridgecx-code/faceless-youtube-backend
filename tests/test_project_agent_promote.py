from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import tools.project_agent_runtime.promote as promote_module
from tests.test_project_agent_promotion import _published
from tests.test_project_agent_publish import BRANCH, _bare_sha, _git, _make_sibling_commit
from tools.project_agent_runtime.operation_lease import operation_lease
from tools.project_agent_runtime.promote import (
    DEFAULT_PROMOTION_VALIDATIONS,
    PromotionTransactionError,
    execute_promotion,
)
from tools.project_agent_runtime.run_manifest import run_directory


PASS_VALIDATION = (
    (
        sys.executable,
        "-c",
        "from pathlib import Path; assert Path('app/feature.py').read_text().strip() == 'VALUE = 2'",
    ),
)
FAIL_VALIDATION = ((sys.executable, "-c", "raise SystemExit(7)"),)


def _promotion_fixture(tmp_path: Path):
    control, executor, config, manifest, run, checkpoint, bare, base = _published(tmp_path)
    return control, executor, config, manifest, run, checkpoint, bare, base


def test_promotion_transaction_uses_ff_only_validation_and_cas(tmp_path: Path) -> None:
    control, executor, config, _, run, checkpoint, bare, base = _promotion_fixture(tmp_path)
    executor_before = (
        _git(executor, "rev-parse", "HEAD"),
        _git(executor, "branch", "--show-current"),
        _git(executor, "status", "--porcelain=v1", "-uall"),
    )

    result = execute_promotion(
        control,
        config,
        run.run_id,
        _remote_target=str(bare),
        _validation_venv=None,
        _validation_commands=PASS_VALIDATION,
    )

    assert result.status == "PROMOTED"
    assert result.canonical_before == base
    assert result.canonical_after == checkpoint.commit_sha
    assert _bare_sha(bare, config.canonical_branch) == checkpoint.commit_sha
    assert _bare_sha(bare, BRANCH) == checkpoint.commit_sha
    assert all(item.passed for item in result.validations)
    assert (
        _git(executor, "rev-parse", "HEAD"),
        _git(executor, "branch", "--show-current"),
        _git(executor, "status", "--porcelain=v1", "-uall"),
    ) == executor_before

    intent = json.loads(Path(result.intent_path).read_text(encoding="utf-8"))
    evidence = json.loads(Path(result.evidence_path).read_text(encoding="utf-8"))
    assert intent["merge_mode"] == "ff-only"
    assert intent["merge_commit_created"] is False
    assert intent["validation_passed"] is True
    assert evidence["status"] == "PROMOTED"
    assert evidence["canonical_before"] == base
    assert evidence["canonical_after"] == checkpoint.commit_sha
    work_root = Path(config.state_dir).expanduser().resolve() / "promotion-work"
    assert work_root.is_dir()
    assert list(work_root.iterdir()) == []


def test_promotion_transaction_is_idempotent_after_success(tmp_path: Path) -> None:
    control, _, config, _, run, checkpoint, bare, _ = _promotion_fixture(tmp_path)
    first = execute_promotion(
        control,
        config,
        run.run_id,
        _remote_target=str(bare),
        _validation_venv=None,
        _validation_commands=PASS_VALIDATION,
    )
    intent_bytes = Path(first.intent_path).read_bytes()
    evidence_bytes = Path(first.evidence_path).read_bytes()

    second = execute_promotion(
        control,
        config,
        run.run_id,
        _remote_target=str(bare),
        _validation_venv=None,
        _validation_commands=PASS_VALIDATION,
    )
    assert second.status == "ALREADY_PROMOTED"
    assert second.canonical_after == checkpoint.commit_sha
    assert Path(first.intent_path).read_bytes() == intent_bytes
    assert Path(first.evidence_path).read_bytes() == evidence_bytes


def test_promotion_validation_failure_never_moves_canonical_or_writes_intent(tmp_path: Path) -> None:
    control, _, config, _, run, _, bare, base = _promotion_fixture(tmp_path)
    with pytest.raises(PromotionTransactionError, match="validation failed"):
        execute_promotion(
            control,
            config,
            run.run_id,
            _remote_target=str(bare),
            _validation_venv=None,
            _validation_commands=FAIL_VALIDATION,
        )
    assert _bare_sha(bare, config.canonical_branch) == base
    run_root = run_directory(control, config, run.run_id)
    assert not (run_root / "promotion-intent.json").exists()
    assert not (run_root / "promotion.json").exists()


def test_promotion_rejects_external_already_promoted_without_intent(tmp_path: Path) -> None:
    control, executor, config, _, run, checkpoint, bare, _ = _promotion_fixture(tmp_path)
    _git(
        executor,
        "push",
        str(bare),
        f"{checkpoint.commit_sha}:refs/heads/{config.canonical_branch}",
    )
    with pytest.raises(PromotionTransactionError, match="no YouMo promotion intent"):
        execute_promotion(
            control,
            config,
            run.run_id,
            _remote_target=str(bare),
            _validation_venv=None,
            _validation_commands=PASS_VALIDATION,
        )


def test_promotion_recovers_evidence_after_push_succeeded_but_final_journal_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control, _, config, _, run, checkpoint, bare, _ = _promotion_fixture(tmp_path)
    original_create = promote_module._atomic_create_json

    def fail_final(path: Path, payload: dict[str, object]) -> None:
        if path.name == "promotion.json":
            raise PromotionTransactionError("simulated final journal failure")
        original_create(path, payload)

    monkeypatch.setattr(promote_module, "_atomic_create_json", fail_final)
    with pytest.raises(PromotionTransactionError, match="simulated final journal failure"):
        execute_promotion(
            control,
            config,
            run.run_id,
            _remote_target=str(bare),
            _validation_venv=None,
            _validation_commands=PASS_VALIDATION,
        )
    run_root = run_directory(control, config, run.run_id)
    assert (run_root / "promotion-intent.json").is_file()
    assert not (run_root / "promotion.json").exists()
    assert _bare_sha(bare, config.canonical_branch) == checkpoint.commit_sha

    monkeypatch.setattr(promote_module, "_atomic_create_json", original_create)
    recovered = execute_promotion(
        control,
        config,
        run.run_id,
        _remote_target=str(bare),
        _validation_venv=None,
        _validation_commands=PASS_VALIDATION,
    )
    assert recovered.status == "RECOVERED_PROMOTION_EVIDENCE"
    assert Path(recovered.evidence_path).is_file()
    assert _bare_sha(bare, config.canonical_branch) == checkpoint.commit_sha


def test_promotion_cas_rejects_canonical_race_after_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control, _, config, _, run, _, bare, base = _promotion_fixture(tmp_path)
    racer, racer_sha = _make_sibling_commit(tmp_path, control, base, "promotion-racer")
    original_create = promote_module._atomic_create_json
    raced = False

    def race_after_intent(path: Path, payload: dict[str, object]) -> None:
        nonlocal raced
        original_create(path, payload)
        if path.name == "promotion-intent.json" and not raced:
            raced = True
            _git(
                racer,
                "push",
                str(bare),
                f"{racer_sha}:refs/heads/{config.canonical_branch}",
            )

    monkeypatch.setattr(promote_module, "_atomic_create_json", race_after_intent)
    with pytest.raises(PromotionTransactionError, match="failed with exit"):
        execute_promotion(
            control,
            config,
            run.run_id,
            _remote_target=str(bare),
            _validation_venv=None,
            _validation_commands=PASS_VALIDATION,
        )
    assert raced
    assert _bare_sha(bare, config.canonical_branch) == racer_sha
    run_root = run_directory(control, config, run.run_id)
    assert (run_root / "promotion-intent.json").is_file()
    assert not (run_root / "promotion.json").exists()


def test_promotion_refuses_diverged_canonical(tmp_path: Path) -> None:
    control, _, config, _, run, _, bare, base = _promotion_fixture(tmp_path)
    sibling, sibling_sha = _make_sibling_commit(tmp_path, control, base, "promotion-diverged")
    _git(
        sibling,
        "push",
        str(bare),
        f"{sibling_sha}:refs/heads/{config.canonical_branch}",
    )
    with pytest.raises(PromotionTransactionError, match="not eligible: BLOCKED_DIVERGED"):
        execute_promotion(
            control,
            config,
            run.run_id,
            _remote_target=str(bare),
            _validation_venv=None,
            _validation_commands=PASS_VALIDATION,
        )
    assert _bare_sha(bare, config.canonical_branch) == sibling_sha


def test_promotion_respects_executor_lease(tmp_path: Path) -> None:
    control, executor, config, _, run, _, bare, _ = _promotion_fixture(tmp_path)
    with operation_lease(control, config, executor, "external-test"):
        with pytest.raises(PromotionTransactionError, match="lease"):
            execute_promotion(
                control,
                config,
                run.run_id,
                _remote_target=str(bare),
                _validation_venv=None,
                _validation_commands=PASS_VALIDATION,
            )


def test_default_promotion_validation_is_full_repository_gate() -> None:
    assert DEFAULT_PROMOTION_VALIDATIONS == (
        (".venv/bin/python", "-m", "pytest", "-q"),
        ("node", "--check", "app/static/app.js"),
    )
