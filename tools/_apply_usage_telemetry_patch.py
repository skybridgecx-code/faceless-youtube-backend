from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def replace_once(relative: str, old: str, new: str) -> None:
    path = ROOT / relative
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{relative}: expected one replacement target, found {count}")
    path.write_text(text.replace(old, new), encoding="utf-8")


# Harden the usage helper with replay-safe timestamp validation and expose the
# snapshot-based calculation so its historical reproducibility is testable.
replace_once(
    "tools/project_agent_runtime/usage.py",
    "def _estimate_credits(\n",
    "def estimate_credits(\n",
)
replace_once(
    "tools/project_agent_runtime/usage.py",
    "        credits = _estimate_credits(\n",
    "        credits = estimate_credits(\n",
)
replace_once(
    "tools/project_agent_runtime/usage.py",
    "            timestamp=_required_string(value.get(\"timestamp\"), \"usage timestamp\"),\n",
    "            timestamp=_validated_timestamp(value.get(\"timestamp\")),\n",
)
replace_once(
    "tools/project_agent_runtime/usage.py",
    "def _utc_now() -> str:\n    return datetime.now(timezone.utc).isoformat()\n\n\ndef credit_rate_snapshot",
    "def _utc_now() -> str:\n    return datetime.now(timezone.utc).isoformat()\n\n\ndef _validated_timestamp(value: object) -> str:\n    raw = _required_string(value, \"usage timestamp\")\n    try:\n        observed = datetime.fromisoformat(raw)\n    except ValueError as exc:\n        raise UsageError(\"usage timestamp must be ISO-8601\") from exc\n    if observed.tzinfo is None:\n        raise UsageError(\"usage timestamp must include a timezone\")\n    return raw\n\n\ndef credit_rate_snapshot",
)

# Build evidence retains per-turn usage.
replace_once(
    "tools/project_agent_runtime/build_engine.py",
    "from typing import Any, Awaitable, Callable, Sequence\n\n\nclass BuildGuardError",
    "from typing import Any, Awaitable, Callable, Sequence\n\nfrom .usage import UsageRecord, capture_turn_usage\n\n\nclass BuildGuardError",
)
replace_once(
    "tools/project_agent_runtime/build_engine.py",
    "    model_response: str | None\n\n    @property",
    "    model_response: str | None\n    usage: UsageRecord | None = None\n\n    @property",
)
replace_once(
    "tools/project_agent_runtime/build_engine.py",
    "    turn_runner: TurnRunner,\n    max_changed_files: int = 20,",
    "    turn_runner: TurnRunner,\n    run_id: str | None = None,\n    max_changed_files: int = 20,",
)
replace_once(
    "tools/project_agent_runtime/build_engine.py",
    "    violations: list[str] = []\n    branch_after = _branch(root)",
    "    usage = capture_turn_usage(\n        getattr(result, \"usage\", None),\n        run_id=run_id,\n        stage=\"build\",\n        model=model,\n        reasoning_effort=reasoning,\n    )\n\n    violations: list[str] = []\n    branch_after = _branch(root)",
)
replace_once(
    "tools/project_agent_runtime/build_engine.py",
    "        model_response=getattr(result, \"final_response\", None),\n    )",
    "        model_response=getattr(result, \"final_response\", None),\n        usage=usage,\n    )",
)

# Audit evidence retains per-turn usage when a Codex turn actually runs.
replace_once(
    "tools/project_agent_runtime/audit_engine.py",
    "from .build_engine import (\n",
    "from .usage import UsageRecord, capture_turn_usage\n\nfrom .build_engine import (\n",
)
replace_once(
    "tools/project_agent_runtime/audit_engine.py",
    "    raw_response: str | None\n\n    @property",
    "    raw_response: str | None\n    usage: UsageRecord | None = None\n\n    @property",
)
replace_once(
    "tools/project_agent_runtime/audit_engine.py",
    "    turn_runner: TurnRunner,\n    validation_commands: Sequence[Sequence[str]] = DEFAULT_VALIDATION_COMMANDS,",
    "    turn_runner: TurnRunner,\n    run_id: str | None = None,\n    validation_commands: Sequence[Sequence[str]] = DEFAULT_VALIDATION_COMMANDS,",
)
replace_once(
    "tools/project_agent_runtime/audit_engine.py",
    "            raw_response=None,\n        )",
    "            raw_response=None, usage=None,\n        )",
)
replace_once(
    "tools/project_agent_runtime/audit_engine.py",
    "    violations: list[str] = []\n    turn_status = str(getattr(turn, \"status\", \"unknown\"))",
    "    usage = capture_turn_usage(\n        getattr(turn, \"usage\", None),\n        run_id=run_id,\n        stage=\"audit\",\n        model=model,\n        reasoning_effort=reasoning,\n    )\n\n    violations: list[str] = []\n    turn_status = str(getattr(turn, \"status\", \"unknown\"))",
)
replace_once(
    "tools/project_agent_runtime/audit_engine.py",
    "        raw_response=raw,\n    )",
    "        raw_response=raw,\n        usage=usage,\n    )",
)

# Bind usage records to the immutable run id at execution time.
replace_once(
    "tools/project_agent_runtime/build_cli.py",
    "                    turn_runner=run_codex_turn,\n                    max_changed_files=manifest.max_changed_files,",
    "                    turn_runner=run_codex_turn,\n                    run_id=manifest.run_id,\n                    max_changed_files=manifest.max_changed_files,",
)
replace_once(
    "tools/project_agent_runtime/audit_cli.py",
    "                        turn_runner=run_codex_turn,\n                        validation_commands=_validation_commands(bound),",
    "                        turn_runner=run_codex_turn,\n                        run_id=manifest.run_id if manifest is not None else None,\n                        validation_commands=_validation_commands(bound),",
)

# Replace overview with a read-only usage-aware aggregation layer. It resolves
# and hash-verifies bound evidence before using telemetry and never writes state.
overview = '''from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .config import ProjectConfig
from .doctor import diagnose_workspace
from .run_manifest import (
    EvidenceBinding,
    RunManifest,
    RunManifestError,
    load_run_manifest,
    resolve_evidence,
    run_directory,
)
from .state import state_directory
from .usage import (
    CACHE_RATIO_DEFINITION,
    UsageError,
    UsageRecord,
    UsageTotals,
    UsageWarningThresholds,
    aggregate_usage,
    load_usage_record,
    usage_by_model,
    usage_for_utc_date,
    usage_warnings,
    warning_thresholds_from_env,
)


class OverviewError(RuntimeError):
    """Raised when local YouMo operator state cannot be summarized safely."""


_RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SAFE_FLOW_STATES = {
    "READY_FOR_AUDIT",
    "READY_FOR_CHECKPOINT",
    "CHECKPOINT_RETRYABLE",
    "CHECKPOINTED_CLEAN",
}


@dataclass(frozen=True)
class RunOverview:
    run_id: str
    workspace: str
    branch: str
    stage: str
    doctor_state: str
    detail: str
    latest_for_workspace: bool
    safe_to_flow: bool
    next_command: str
    workspace_exists: bool
    cleanup_classification: str
    build_usage: UsageRecord | None
    audit_usage: UsageRecord | None
    usage_total: UsageTotals
    usage_warnings: tuple[str, ...]


@dataclass(frozen=True)
class OverviewReport:
    state_root: str
    discovered_runs: int
    workspaces: int
    latest_runs: int
    safe_to_flow: int
    blocked_latest_runs: int
    missing_workspaces: int
    historical_runs: int
    usage_all_time: UsageTotals
    usage_today_utc: UsageTotals
    usage_today_utc_date: str
    usage_by_model: dict[str, UsageTotals]
    usage_warning_thresholds: UsageWarningThresholds
    usage_warnings: tuple[str, ...]
    cache_ratio_definition: str
    items: tuple[RunOverview, ...]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\\n"


def _candidate_run_ids(state_root: Path) -> tuple[str, ...]:
    if not state_root.exists():
        return ()
    if not state_root.is_dir():
        raise OverviewError(f"YouMo state root is not a directory: {state_root}")
    values: set[str] = set()
    try:
        for path in state_root.rglob("*"):
            if path.is_dir() and not path.is_symlink() and _RUN_ID_RE.fullmatch(path.name):
                values.add(path.name)
    except OSError as exc:
        raise OverviewError(f"failed to scan YouMo state root: {state_root}") from exc
    return tuple(sorted(values))


def discover_run_manifests(
    control_root: Path,
    config: ProjectConfig,
) -> tuple[RunManifest, ...]:
    root = state_directory(control_root.resolve(), config.state_dir)
    manifests: list[RunManifest] = []
    for run_id in _candidate_run_ids(root):
        try:
            manifests.append(load_run_manifest(control_root, config, run_id))
        except RunManifestError:
            # 32-hex directories may belong to other state artifacts. Only valid
            # immutable run manifests participate in the operator overview.
            continue
    return tuple(manifests)


def _run_mtime(control_root: Path, config: ProjectConfig, manifest: RunManifest) -> int:
    try:
        return run_directory(control_root, config, manifest.run_id).stat().st_mtime_ns
    except OSError:
        return 0


def _latest_by_workspace(
    control_root: Path,
    config: ProjectConfig,
    manifests: Iterable[RunManifest],
) -> dict[str, str]:
    latest: dict[str, tuple[int, str]] = {}
    for manifest in manifests:
        workspace = str(Path(manifest.workspace).expanduser().resolve())
        candidate = (_run_mtime(control_root, config, manifest), manifest.run_id)
        current = latest.get(workspace)
        if current is None or candidate > current:
            latest[workspace] = candidate
    return {workspace: run_id for workspace, (_, run_id) in latest.items()}


def _next_command(manifest: RunManifest, doctor_state: str) -> str:
    if doctor_state in _SAFE_FLOW_STATES:
        return (
            f"youmo-flow --run-id {manifest.run_id} "
            "--until promotion-check --execute"
        )
    workspace = str(Path(manifest.workspace).expanduser().resolve())
    return f"youmo-doctor --workspace {json.dumps(workspace)}"


def _bound_usage(
    control_root: Path,
    config: ProjectConfig,
    manifest: RunManifest,
    binding: EvidenceBinding | None,
    *,
    expected_stage: str,
) -> UsageRecord | None:
    if binding is None:
        return None
    try:
        path = resolve_evidence(
            binding,
            expected_parent=run_directory(control_root, config, manifest.run_id),
        )
        record = load_usage_record(path)
    except (RunManifestError, UsageError) as exc:
        raise OverviewError(
            f"usage evidence verification failed for run {manifest.run_id} {expected_stage}: {exc}"
        ) from exc
    # Legacy evidence created before telemetry is valid and remains immutable.
    if record is None:
        return None
    if record.run_id != manifest.run_id:
        raise OverviewError(
            f"usage run_id mismatch for {expected_stage}: {record.run_id!r} != {manifest.run_id!r}"
        )
    if record.stage != expected_stage:
        raise OverviewError(
            f"usage stage mismatch for run {manifest.run_id}: {record.stage!r} != {expected_stage!r}"
        )
    return record


def build_overview(
    control_root: Path,
    config: ProjectConfig,
    *,
    include_history: bool = False,
) -> OverviewReport:
    control = control_root.resolve()
    root = state_directory(control, config.state_dir)
    manifests = discover_run_manifests(control, config)
    latest = _latest_by_workspace(control, config, manifests)
    try:
        thresholds = warning_thresholds_from_env()
    except UsageError as exc:
        raise OverviewError(f"usage warning threshold configuration is invalid: {exc}") from exc

    usage_by_run: dict[str, tuple[UsageRecord | None, UsageRecord | None]] = {}
    all_records: list[UsageRecord] = []
    report_warnings: list[str] = []
    for manifest in manifests:
        build_usage = _bound_usage(
            control, config, manifest, manifest.build_evidence, expected_stage="build"
        )
        audit_usage = _bound_usage(
            control, config, manifest, manifest.audit_evidence, expected_stage="audit"
        )
        usage_by_run[manifest.run_id] = (build_usage, audit_usage)
        run_records = tuple(record for record in (build_usage, audit_usage) if record is not None)
        all_records.extend(run_records)
        for warning in usage_warnings(run_records, thresholds):
            report_warnings.append(f"run {manifest.run_id}: {warning}")

    today = datetime.now(timezone.utc).date()
    all_tuple = tuple(all_records)
    all_time = aggregate_usage(all_tuple)
    today_usage = aggregate_usage(usage_for_utc_date(all_tuple, today))
    by_model = usage_by_model(all_tuple)
    items: list[RunOverview] = []

    for manifest in manifests:
        workspace_path = Path(manifest.workspace).expanduser().resolve()
        workspace = str(workspace_path)
        is_latest = latest.get(workspace) == manifest.run_id
        if not include_history and not is_latest:
            continue
        exists = workspace_path.is_dir()
        try:
            diagnosis = diagnose_workspace(control, config, workspace_path)
            doctor_state = diagnosis.state
            detail = diagnosis.detail
        except Exception as exc:
            doctor_state = "DOCTOR_ERROR"
            detail = f"doctor failed: {exc}"
        safe = is_latest and doctor_state in _SAFE_FLOW_STATES
        if not exists:
            cleanup = "MISSING_WORKSPACE_REGISTRATION_REVIEW"
        elif not is_latest:
            cleanup = "HISTORICAL_RUN_KEEP_EVIDENCE"
        else:
            cleanup = "KEEP"
        build_usage, audit_usage = usage_by_run[manifest.run_id]
        run_records = tuple(record for record in (build_usage, audit_usage) if record is not None)
        items.append(
            RunOverview(
                run_id=manifest.run_id,
                workspace=workspace,
                branch=manifest.branch,
                stage=manifest.stage,
                doctor_state=doctor_state,
                detail=detail,
                latest_for_workspace=is_latest,
                safe_to_flow=safe,
                next_command=_next_command(manifest, doctor_state),
                workspace_exists=exists,
                cleanup_classification=cleanup,
                build_usage=build_usage,
                audit_usage=audit_usage,
                usage_total=aggregate_usage(run_records),
                usage_warnings=usage_warnings(run_records, thresholds),
            )
        )

    items.sort(key=lambda item: (not item.latest_for_workspace, item.workspace, item.run_id))
    latest_items = [item for item in items if item.latest_for_workspace]
    return OverviewReport(
        state_root=str(root),
        discovered_runs=len(manifests),
        workspaces=len(latest),
        latest_runs=len(latest_items),
        safe_to_flow=sum(1 for item in latest_items if item.safe_to_flow),
        blocked_latest_runs=sum(1 for item in latest_items if not item.safe_to_flow),
        missing_workspaces=sum(1 for item in latest_items if not item.workspace_exists),
        historical_runs=max(0, len(manifests) - len(latest)),
        usage_all_time=all_time,
        usage_today_utc=today_usage,
        usage_today_utc_date=today.isoformat(),
        usage_by_model=by_model,
        usage_warning_thresholds=thresholds,
        usage_warnings=tuple(report_warnings),
        cache_ratio_definition=CACHE_RATIO_DEFINITION,
        items=tuple(items),
    )
'''
(ROOT / "tools/project_agent_runtime/overview.py").write_text(overview, encoding="utf-8")

overview_cli = '''from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Sequence

from .config import ProjectConfigError, load_project_config
from .gates import run_preflight_gate
from .git_state import GitInspectionError, inspect_repo
from .overview import OverviewError, build_overview
from .usage import UsageRecord, UsageTotals

DEFAULT_MANIFEST = "tools/project_agent_runtime/projects/youmo.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="youmo-overview",
        description=(
            "Read-only local overview of YouMo executor/run state, usage telemetry, and the next safe command. "
            "No remote access, cleanup, Git mutation, or Codex execution is performed."
        ),
    )
    parser.add_argument("--repo", default=".", help="read-only YouMo controller repository")
    parser.add_argument("--project", default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--history",
        action="store_true",
        help="include superseded historical run journals in addition to latest per executor",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def _usage_text(record: UsageRecord | None) -> str:
    if record is None:
        return "NOT_RECORDED"
    if not record.usage_available:
        return f"UNAVAILABLE|MODEL:{record.model}|REASONING:{record.reasoning_effort}"
    credits = "UNPRICED" if record.estimated_credits is None else f"{record.estimated_credits:.6f}"
    return (
        f"INPUT:{record.input_tokens}|CACHED:{record.cached_input_tokens}|"
        f"OUTPUT:{record.output_tokens}|CREDITS:{credits}|MODEL:{record.model}|"
        f"REASONING:{record.reasoning_effort}"
    )


def _totals_text(totals: UsageTotals) -> str:
    ratio = "NONE" if totals.cache_ratio is None else f"{totals.cache_ratio:.6f}"
    completeness = "COMPLETE" if totals.estimated_credits_complete else "KNOWN_ONLY"
    return (
        f"INPUT:{totals.input_tokens}|CACHED:{totals.cached_input_tokens}|"
        f"OUTPUT:{totals.output_tokens}|CREDITS:{totals.estimated_credits:.6f}|"
        f"CREDIT_STATUS:{completeness}|CACHE_RATIO:{ratio}|TURNS:{totals.turns}|"
        f"AVAILABLE:{totals.available_turns}|UNAVAILABLE:{totals.unavailable_turns}|"
        f"UNPRICED:{totals.unpriced_turns}"
    )


def _model_label(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", model).strip("_").upper() or "UNKNOWN"


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        control_state = inspect_repo(Path(args.repo).resolve())
        config = load_project_config(control_state.root, args.project)
    except (GitInspectionError, ProjectConfigError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    preflight = run_preflight_gate(control_state.root, config, control_state)
    if not preflight.passed:
        print("STOP: controller preflight failed.", file=sys.stderr)
        for check in preflight.checks:
            if not check.passed:
                print(f"FAIL {check.name}: {check.detail}", file=sys.stderr)
        return 2

    try:
        report = build_overview(
            control_state.root,
            config,
            include_history=args.history,
        )
    except OverviewError as exc:
        print(f"STOP: overview failed: {exc}", file=sys.stderr)
        return 16

    if args.json:
        print(report.to_json(), end="")
        return 0

    print("YOUMO_OVERVIEW=LOCAL_READ_ONLY")
    print(f"STATE_ROOT={report.state_root}")
    print(f"DISCOVERED_RUNS={report.discovered_runs}")
    print(f"WORKSPACES={report.workspaces}")
    print(f"LATEST_RUNS={report.latest_runs}")
    print(f"SAFE_TO_FLOW={report.safe_to_flow}")
    print(f"BLOCKED_LATEST_RUNS={report.blocked_latest_runs}")
    print(f"MISSING_WORKSPACES={report.missing_workspaces}")
    print(f"HISTORICAL_RUNS={report.historical_runs}")
    print(f"USAGE_CACHE_RATIO_DEFINITION={report.cache_ratio_definition}")
    turn_threshold = report.usage_warning_thresholds.turn_credits
    run_threshold = report.usage_warning_thresholds.run_credits
    print(f"USAGE_WARN_TURN_CREDITS={turn_threshold if turn_threshold is not None else '<disabled>'}")
    print(f"USAGE_WARN_RUN_CREDITS={run_threshold if run_threshold is not None else '<disabled>'}")
    print(f"USAGE_ALL_TIME={_totals_text(report.usage_all_time)}")
    print(f"USAGE_TODAY_UTC_DATE={report.usage_today_utc_date}")
    print(f"USAGE_TODAY_UTC={_totals_text(report.usage_today_utc)}")
    for model, totals in report.usage_by_model.items():
        print(f"USAGE_MODEL_{_model_label(model)}={_totals_text(totals)}")
    for warning in report.usage_warnings:
        print(f"USAGE_WARNING={warning}")
    for index, item in enumerate(report.items, start=1):
        print(f"ITEM_{index}_RUN_ID={item.run_id}")
        print(f"ITEM_{index}_WORKSPACE={item.workspace}")
        print(f"ITEM_{index}_BRANCH={item.branch}")
        print(f"ITEM_{index}_STAGE={item.stage}")
        print(f"ITEM_{index}_DOCTOR_STATE={item.doctor_state}")
        print(f"ITEM_{index}_LATEST={str(item.latest_for_workspace).upper()}")
        print(f"ITEM_{index}_SAFE_TO_FLOW={str(item.safe_to_flow).upper()}")
        print(f"ITEM_{index}_CLEANUP={item.cleanup_classification}")
        print(f"ITEM_{index}_NEXT={item.next_command}")
        print(f"ITEM_{index}_DETAIL={item.detail}")
        print(f"ITEM_{index}_BUILD_USAGE={_usage_text(item.build_usage)}")
        print(f"ITEM_{index}_AUDIT_USAGE={_usage_text(item.audit_usage)}")
        print(f"ITEM_{index}_USAGE_TOTAL={_totals_text(item.usage_total)}")
        for warning in item.usage_warnings:
            print(f"ITEM_{index}_USAGE_WARNING={warning}")
    print("NETWORK_ACCESS=NONE")
    print("CLEANUP_EXECUTION=NONE")
    print("GIT_MUTATION=NONE")
    print("CODEX_TRANSPORT=NOT_STARTED")
    print("CANONICAL_PROMOTION=NOT_STARTED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''
(ROOT / "tools/project_agent_runtime/overview_cli.py").write_text(overview_cli, encoding="utf-8")

usage_tests = '''from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_project_agent_overview import _tree_fingerprint
from tests.test_project_agent_runs import _fixture
from tools.project_agent_runtime.architecture import load_architecture_snapshot
from tools.project_agent_runtime.audit_engine import run_guarded_audit
from tools.project_agent_runtime.build_engine import (
    architecture_fingerprint,
    changed_files,
    diff_fingerprint,
    run_guarded_build,
)
from tools.project_agent_runtime.overview import build_overview
from tools.project_agent_runtime.overview_cli import main as overview_main
from tools.project_agent_runtime.run_manifest import (
    RunManifestError,
    create_run_manifest,
    resolve_evidence,
    run_directory,
    transition_run,
    write_run_evidence,
)
from tools.project_agent_runtime.usage import (
    CACHE_RATIO_DEFINITION,
    UsageError,
    aggregate_usage,
    capture_turn_usage,
    credit_rate_snapshot,
    estimate_credits,
    usage_warnings,
    warning_thresholds_from_env,
)


def _raw_usage(*, input_tokens: int, cached_tokens: int, output_tokens: int) -> dict[str, object]:
    return {
        "total": {
            "total_tokens": 9999999,
            "input_tokens": 8888888,
            "cached_input_tokens": 7777777,
            "cache_write_input_tokens": 10,
            "output_tokens": 6666666,
            "reasoning_output_tokens": 5555555,
        },
        "last": {
            "total_tokens": input_tokens + output_tokens,
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_tokens,
            "cache_write_input_tokens": 7,
            "output_tokens": output_tokens,
            "reasoning_output_tokens": 11,
        },
        "model_context_window": 200000,
    }


def test_sdk_usage_extraction_uses_last_turn_and_preserves_raw_breakdowns() -> None:
    record = capture_turn_usage(
        _raw_usage(input_tokens=1_000_000, cached_tokens=400_000, output_tokens=100_000),
        run_id="a" * 32,
        stage="build",
        model="gpt-5.6-terra",
        reasoning_effort="high",
        timestamp="2026-08-11T20:00:00+00:00",
    )
    assert record.usage_available
    assert record.input_tokens == 1_000_000
    assert record.cached_input_tokens == 400_000
    assert record.output_tokens == 100_000
    assert record.sdk_total_tokens == 1_100_000
    assert record.sdk_reasoning_output_tokens == 11
    assert record.sdk_cache_write_input_tokens == 7
    assert record.sdk_model_context_window == 200000
    assert record.estimated_credits == 77.5
    assert record.credit_rate_snapshot.input_credits_per_million == 62.5
    assert record.credit_rate_snapshot.cached_input_credits_per_million == 6.25
    assert record.credit_rate_snapshot.output_credits_per_million == 375.0


def test_missing_sdk_usage_is_explicit_and_never_invented() -> None:
    record = capture_turn_usage(
        None,
        run_id="b" * 32,
        stage="audit",
        model="gpt-5.6-sol",
        reasoning_effort="high",
        timestamp="2026-08-11T20:00:00+00:00",
    )
    assert not record.usage_available
    assert record.input_tokens is None
    assert record.cached_input_tokens is None
    assert record.output_tokens is None
    assert record.estimated_credits is None
    assert record.sdk_total_tokens is None


def test_credit_calculation_is_reproducible_from_captured_rate_snapshot() -> None:
    snapshot = credit_rate_snapshot("gpt-5.6-sol")
    assert snapshot.version == "openai-codex-rate-card-2026-08-11"
    assert estimate_credits(
        input_tokens=1_000_000,
        cached_input_tokens=400_000,
        output_tokens=100_000,
        snapshot=snapshot,
    ) == 155.0
    unknown = credit_rate_snapshot("gpt-unknown")
    assert estimate_credits(
        input_tokens=100,
        cached_input_tokens=10,
        output_tokens=5,
        snapshot=unknown,
    ) is None


def _architecture_paths(config) -> tuple[str, ...]:
    return (config.architecture_lock, *config.architecture_sources)


def _built_run(tmp_path: Path):
    control, executor, config, manifest_path, base = _fixture(tmp_path)
    task = "Implement telemetry persistence"
    architecture = load_architecture_snapshot(control, config)
    run = create_run_manifest(
        control,
        config,
        workspace=executor,
        branch="phase/run-test",
        base_head=base,
        task=task,
        allowed_paths=("app",),
        max_changed_files=10,
        architecture_lock_sha256=architecture.lock_sha256,
    )

    async def build_turn(**kwargs: object) -> SimpleNamespace:
        (executor / "app" / "feature.py").write_text("VALUE = 2\\n", encoding="utf-8")
        return SimpleNamespace(
            thread_id="usage-build-thread",
            turn_id="usage-build-turn",
            status="completed",
            final_response="implemented",
            usage=_raw_usage(input_tokens=1000, cached_tokens=400, output_tokens=100),
        )

    result = asyncio.run(
        run_guarded_build(
            workspace_root=executor,
            task=task,
            allowed_paths=("app",),
            architecture_paths=_architecture_paths(config),
            developer_instructions="rules",
            model="gpt-5.6-terra",
            reasoning="high",
            turn_runner=build_turn,
            run_id=run.run_id,
            max_changed_files=10,
            validation_commands=(("python3", "-c", "pass"),),
        )
    )
    assert result.ready_for_audit
    binding = write_run_evidence(
        control, config, run, stage_name="build", payload=result.to_json()
    )
    ready = transition_run(
        control, config, run, new_stage="READY_FOR_AUDIT", build_evidence=binding
    )
    return control, executor, config, manifest_path, ready, result


def test_build_usage_is_persisted_inside_immutable_hash_bound_evidence(tmp_path: Path) -> None:
    control, _, config, _, run, result = _built_run(tmp_path)
    assert result.usage is not None
    assert result.usage.run_id == run.run_id
    assert result.usage.stage == "build"
    assert run.build_evidence is not None
    parent = run_directory(control, config, run.run_id)
    path = resolve_evidence(run.build_evidence, expected_parent=parent)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["usage"]["run_id"] == run.run_id
    assert payload["usage"]["stage"] == "build"
    assert payload["usage"]["input_tokens"] == 1000
    before = path.read_bytes()
    with pytest.raises(RunManifestError, match="refusing to overwrite immutable build evidence"):
        write_run_evidence(control, config, run, stage_name="build", payload="{}\\n")
    assert path.read_bytes() == before


def test_audit_usage_is_persisted_separately_and_hash_bound(tmp_path: Path) -> None:
    control, executor, config, _, ready, build_result = _built_run(tmp_path)
    assert ready.build_evidence is not None
    build_path = resolve_evidence(
        ready.build_evidence,
        expected_parent=run_directory(control, config, ready.run_id),
    )
    build_payload = json.loads(build_path.read_text(encoding="utf-8"))
    expected_arch = architecture_fingerprint(control, _architecture_paths(config))

    async def audit_turn(**kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            thread_id="usage-audit-thread",
            turn_id="usage-audit-turn",
            status="completed",
            final_response=json.dumps(
                {"verdict": "PASS", "summary": "clean", "findings": []}
            ),
            usage=_raw_usage(input_tokens=2000, cached_tokens=1000, output_tokens=200),
        )

    running = transition_run(control, config, ready, new_stage="AUDIT_RUNNING")
    audit = asyncio.run(
        run_guarded_audit(
            workspace_root=executor,
            task=ready.task,
            build_evidence=build_payload,
            expected_architecture=expected_arch,
            developer_instructions="rules",
            model="gpt-5.6-sol",
            reasoning="high",
            turn_runner=audit_turn,
            run_id=ready.run_id,
            validation_commands=(("python3", "-c", "pass"),),
        )
    )
    assert audit.passed
    assert audit.usage is not None
    assert audit.usage.run_id == ready.run_id
    assert audit.usage.stage == "audit"
    audit_binding = write_run_evidence(
        control, config, running, stage_name="audit", payload=audit.to_json()
    )
    final = transition_run(
        control,
        config,
        running,
        new_stage="READY_FOR_CHECKPOINT",
        audit_evidence=audit_binding,
    )
    assert final.build_evidence == ready.build_evidence
    assert final.audit_evidence is not None
    audit_path = resolve_evidence(
        final.audit_evidence,
        expected_parent=run_directory(control, config, final.run_id),
    )
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    assert payload["usage"]["stage"] == "audit"
    assert payload["usage"]["model"] == "gpt-5.6-sol"
    assert payload["usage"]["input_tokens"] == 2000
    assert build_result.usage is not None
    assert build_result.usage.model == "gpt-5.6-terra"


def _overview_ready_run(tmp_path: Path):
    control, executor, config, manifest_path, base = _fixture(tmp_path)
    task = "Aggregate immutable usage"
    (executor / "app" / "feature.py").write_text("VALUE = 2\\n", encoding="utf-8")
    files = changed_files(executor)
    diff_sha, fingerprints = diff_fingerprint(executor, files)
    task_sha = hashlib.sha256(task.encode("utf-8")).hexdigest()
    architecture = load_architecture_snapshot(control, config)
    run = create_run_manifest(
        control,
        config,
        workspace=executor,
        branch="phase/run-test",
        base_head=base,
        task=task,
        allowed_paths=("app",),
        max_changed_files=10,
        architecture_lock_sha256=architecture.lock_sha256,
    )
    build_usage = capture_turn_usage(
        _raw_usage(input_tokens=1_000_000, cached_tokens=400_000, output_tokens=100_000),
        run_id=run.run_id,
        stage="build",
        model="gpt-5.6-terra",
        reasoning_effort="high",
        timestamp="2026-08-11T20:00:00+00:00",
    )
    build = {
        "status": "READY_FOR_AUDIT",
        "ready_for_audit": True,
        "base_head": base,
        "branch": "phase/run-test",
        "task_sha256": task_sha,
        "allowed_paths": ["app"],
        "changed_files": list(files),
        "diff_sha256": diff_sha,
        "file_fingerprints": fingerprints,
        "validations": [],
        "violations": [],
        "usage": build_usage.to_mapping(),
    }
    build_binding = write_run_evidence(
        control,
        config,
        run,
        stage_name="build",
        payload=json.dumps(build, indent=2, sort_keys=True) + "\\n",
    )
    ready = transition_run(
        control, config, run, new_stage="READY_FOR_AUDIT", build_evidence=build_binding
    )
    running = transition_run(control, config, ready, new_stage="AUDIT_RUNNING")
    audit_usage = capture_turn_usage(
        _raw_usage(input_tokens=2_000_000, cached_tokens=1_000_000, output_tokens=200_000),
        run_id=run.run_id,
        stage="audit",
        model="gpt-5.6-sol",
        reasoning_effort="high",
        timestamp="2026-08-11T21:00:00+00:00",
    )
    audit = {
        "status": "AUDIT_PASS",
        "passed": True,
        "verdict": "PASS",
        "summary": "clean",
        "findings": [],
        "base_head": base,
        "branch": "phase/run-test",
        "task_sha256": task_sha,
        "diff_sha256": diff_sha,
        "validations": [],
        "violations": [],
        "usage": audit_usage.to_mapping(),
    }
    audit_binding = write_run_evidence(
        control,
        config,
        running,
        stage_name="audit",
        payload=json.dumps(audit, indent=2, sort_keys=True) + "\\n",
    )
    final = transition_run(
        control,
        config,
        running,
        new_stage="READY_FOR_CHECKPOINT",
        audit_evidence=audit_binding,
    )
    return control, config, manifest_path, final


def test_overview_aggregates_run_project_models_cache_ratio_and_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control, config, _, run = _overview_ready_run(tmp_path)
    monkeypatch.setenv("YOUMO_WARN_TURN_CREDITS", "50")
    monkeypatch.setenv("YOUMO_WARN_RUN_CREDITS", "100")
    report = build_overview(control, config)
    assert report.cache_ratio_definition == CACHE_RATIO_DEFINITION
    assert report.usage_all_time.input_tokens == 3_000_000
    assert report.usage_all_time.cached_input_tokens == 1_400_000
    assert report.usage_all_time.output_tokens == 300_000
    assert report.usage_all_time.cache_ratio == round(1_400_000 / 3_000_000, 6)
    assert report.usage_all_time.estimated_credits == 387.5
    assert report.usage_all_time.estimated_credits_complete
    assert set(report.usage_by_model) == {"gpt-5.6-sol", "gpt-5.6-terra"}
    assert report.usage_by_model["gpt-5.6-terra"].estimated_credits == 77.5
    assert report.usage_by_model["gpt-5.6-sol"].estimated_credits == 310.0
    assert len(report.items) == 1
    item = report.items[0]
    assert item.run_id == run.run_id
    assert item.build_usage is not None and item.build_usage.stage == "build"
    assert item.audit_usage is not None and item.audit_usage.stage == "audit"
    assert item.usage_total.estimated_credits == 387.5
    assert len(item.usage_warnings) == 3
    assert len(report.usage_warnings) == 3


def test_overview_json_usage_is_machine_readable_and_does_not_mutate_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    control, config, manifest_path, run = _overview_ready_run(tmp_path)
    state_root = Path(config.state_dir).expanduser().resolve()
    before = _tree_fingerprint(state_root)
    monkeypatch.setenv("YOUMO_WARN_TURN_CREDITS", "50")
    rc = overview_main(
        ["--repo", str(control), "--project", str(manifest_path), "--json"]
    )
    output = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert output["cache_ratio_definition"] == CACHE_RATIO_DEFINITION
    assert output["usage_all_time"]["input_tokens"] == 3_000_000
    assert output["usage_by_model"]["gpt-5.6-terra"]["estimated_credits"] == 77.5
    assert output["items"][0]["run_id"] == run.run_id
    assert output["items"][0]["build_usage"]["credit_rate_snapshot"]["version"] == (
        "openai-codex-rate-card-2026-08-11"
    )
    assert _tree_fingerprint(state_root) == before


def test_warning_thresholds_are_configurable_warnings_not_caps() -> None:
    record = capture_turn_usage(
        _raw_usage(input_tokens=1_000_000, cached_tokens=400_000, output_tokens=100_000),
        run_id="c" * 32,
        stage="build",
        model="gpt-5.6-terra",
        reasoning_effort="high",
        timestamp="2026-08-11T20:00:00+00:00",
    )
    thresholds = warning_thresholds_from_env(
        {"YOUMO_WARN_TURN_CREDITS": "70", "YOUMO_WARN_RUN_CREDITS": "75"}
    )
    warnings = usage_warnings((record,), thresholds)
    assert len(warnings) == 2
    assert "warning threshold" in warnings[0]
    with pytest.raises(UsageError):
        warning_thresholds_from_env({"YOUMO_WARN_TURN_CREDITS": "not-a-number"})
    # Threshold evaluation is observational; the immutable record is unchanged.
    assert aggregate_usage((record,)).estimated_credits == 77.5
'''
(ROOT / "tests/test_project_agent_usage.py").write_text(usage_tests, encoding="utf-8")

print("USAGE_TELEMETRY_PATCH_APPLIED=YES")
