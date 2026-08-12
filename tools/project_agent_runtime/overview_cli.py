from __future__ import annotations

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
