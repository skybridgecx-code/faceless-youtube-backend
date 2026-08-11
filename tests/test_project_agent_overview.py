from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import tools.project_agent_runtime.overview as overview_module
from tests.test_project_agent_runs import _fixture, _ready_run
from tools.project_agent_runtime.overview import build_overview, discover_run_manifests
from tools.project_agent_runtime.overview_cli import main as overview_main


def _tree_fingerprint(root: Path) -> tuple[tuple[str, str], ...]:
    if not root.exists():
        return ()
    values: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            values.append((str(path.relative_to(root)), f"SYMLINK:{path.readlink()}"))
        elif path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            values.append((str(path.relative_to(root)), digest))
        elif path.is_dir():
            values.append((str(path.relative_to(root)) + "/", "DIR"))
    return tuple(values)


def test_overview_empty_state_is_clean_and_read_only(tmp_path: Path) -> None:
    control, _, config, _, _ = _fixture(tmp_path)
    state_root = Path(config.state_dir).expanduser().resolve()
    before = _tree_fingerprint(state_root)

    report = build_overview(control, config)

    assert report.discovered_runs == 0
    assert report.workspaces == 0
    assert report.latest_runs == 0
    assert report.items == ()
    assert _tree_fingerprint(state_root) == before


def test_overview_discovers_real_ready_run_and_recommends_bounded_flow(tmp_path: Path) -> None:
    control, executor, config, _, run, _ = _ready_run(tmp_path)

    manifests = discover_run_manifests(control, config)
    assert [item.run_id for item in manifests] == [run.run_id]

    report = build_overview(control, config)
    assert report.discovered_runs == 1
    assert report.workspaces == 1
    assert report.latest_runs == 1
    assert report.safe_to_flow == 1
    assert report.blocked_latest_runs == 0
    assert len(report.items) == 1
    item = report.items[0]
    assert item.run_id == run.run_id
    assert item.workspace == str(executor.resolve())
    assert item.stage == "READY_FOR_AUDIT"
    assert item.doctor_state == "READY_FOR_AUDIT"
    assert item.latest_for_workspace
    assert item.safe_to_flow
    assert item.cleanup_classification == "KEEP"
    assert item.next_command == (
        f"youmo-flow --run-id {run.run_id} --until promotion-check --execute"
    )


def test_overview_marks_missing_workspace_for_review_without_deleting_evidence(tmp_path: Path) -> None:
    control, executor, config, _, run, _ = _ready_run(tmp_path)
    state_root = Path(config.state_dir).expanduser().resolve()
    executor.rename(tmp_path / "executor-moved-away")
    before = _tree_fingerprint(state_root)

    report = build_overview(control, config)

    assert report.discovered_runs == 1
    assert report.missing_workspaces == 1
    item = report.items[0]
    assert item.run_id == run.run_id
    assert not item.workspace_exists
    assert not item.safe_to_flow
    assert item.cleanup_classification == "MISSING_WORKSPACE_REGISTRATION_REVIEW"
    assert item.next_command.startswith("youmo-doctor --workspace ")
    assert _tree_fingerprint(state_root) == before


def test_overview_cli_reports_explicit_no_mutation_contract(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    control, _, config, manifest_path, run, _ = _ready_run(tmp_path)
    state_root = Path(config.state_dir).expanduser().resolve()
    before = _tree_fingerprint(state_root)

    rc = overview_main(
        ["--repo", str(control), "--project", str(manifest_path)]
    )
    captured = capsys.readouterr()

    assert rc == 0, captured.err
    assert "YOUMO_OVERVIEW=LOCAL_READ_ONLY" in captured.out
    assert f"ITEM_1_RUN_ID={run.run_id}" in captured.out
    assert "NETWORK_ACCESS=NONE" in captured.out
    assert "CLEANUP_EXECUTION=NONE" in captured.out
    assert "GIT_MUTATION=NONE" in captured.out
    assert "CODEX_TRANSPORT=NOT_STARTED" in captured.out
    assert "CANONICAL_PROMOTION=NOT_STARTED" in captured.out
    assert _tree_fingerprint(state_root) == before


def test_overview_json_is_machine_readable_and_local_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    control, _, _, manifest_path, run, _ = _ready_run(tmp_path)
    rc = overview_main(
        ["--repo", str(control), "--project", str(manifest_path), "--json"]
    )
    output = capsys.readouterr().out
    assert rc == 0
    assert f'"run_id": "{run.run_id}"' in output
    assert '"safe_to_flow": 1' in output


def test_overview_source_has_no_network_or_mutating_execution_path() -> None:
    source = Path(overview_module.__file__).read_text(encoding="utf-8")
    forbidden = (
        "subprocess",
        "resolve_remote_branch_sha",
        "execute_publish",
        "execute_promotion",
        "run_codex_turn",
        "git push",
        "git checkout",
        "unlink(",
        "rmtree(",
    )
    for token in forbidden:
        assert token not in source
