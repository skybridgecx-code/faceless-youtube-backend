from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .config import ProjectConfigError, load_project_config
from .gates import run_preflight_gate
from .git_state import GitInspectionError, inspect_repo
from .promotion import PromotionCheckError, check_promotion_readiness

DEFAULT_MANIFEST = "tools/project_agent_runtime/projects/youmo.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="youmo-promote-check",
        description=(
            "Prove whether a published YouMo checkpoint can still be fast-forwarded "
            "from the current canonical branch. This command never merges or pushes."
        ),
    )
    parser.add_argument("--repo", default=".", help="read-only YouMo controller repository")
    parser.add_argument("--project", default=DEFAULT_MANIFEST)
    parser.add_argument("--run-id", required=True, help="published checkpointed YouMo run id")
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
        result = check_promotion_readiness(control_state.root, config, args.run_id)
    except PromotionCheckError as exc:
        print(f"STOP: promotion readiness failed: {exc}", file=sys.stderr)
        return 13

    if args.json:
        print(result.to_json(), end="")
    else:
        print(f"PROMOTION_CLASSIFICATION={result.classification}")
        print(f"RUN_ID={result.run_id}")
        print(f"WORKSPACE={result.workspace}")
        print(f"REPOSITORY={result.repository}")
        print(f"CANDIDATE_BRANCH={result.candidate_branch}")
        print(f"CANDIDATE_SHA={result.candidate_sha}")
        print(f"CANONICAL_BRANCH={result.canonical_branch}")
        print(f"CANONICAL_SHA={result.canonical_sha}")
        print(f"FAST_FORWARD_POSSIBLE={str(result.fast_forward_possible).upper()}")
        print(f"PROMOTION_NEEDED={str(result.promotion_needed).upper()}")
        print(f"CANDIDATE_IN_CANONICAL={str(result.candidate_in_canonical).upper()}")
        print(f"REMOTE_STABLE={str(result.remote_stable).upper()}")
        print("CANONICAL_BRANCH_MUTATION=NONE")
        print("CANDIDATE_BRANCH_MUTATION=NONE")
        print("MERGE=NOT_STARTED")
        print("GIT_PUSH=NOT_STARTED")
        if result.classification == "READY_FAST_FORWARD":
            print("NEXT_ACTION=eligible for a separately authorized ff-only promotion gate")
        elif result.classification == "BLOCKED_DIVERGED":
            print("NEXT_ACTION=create a fresh executor from current canonical; do not auto-rebase")
        else:
            print("NEXT_ACTION=no promotion write is required")

    return 13 if result.blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
