from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .config import ProjectConfigError, load_project_config
from .gates import run_preflight_gate
from .git_state import GitInspectionError, inspect_repo
from .overview import OverviewError, build_overview

DEFAULT_MANIFEST = "tools/project_agent_runtime/projects/youmo.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="youmo-overview",
        description=(
            "Read-only local overview of YouMo executor/run state and the next safe command. "
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
    print("NETWORK_ACCESS=NONE")
    print("CLEANUP_EXECUTION=NONE")
    print("GIT_MUTATION=NONE")
    print("CODEX_TRANSPORT=NOT_STARTED")
    print("CANONICAL_PROMOTION=NOT_STARTED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
