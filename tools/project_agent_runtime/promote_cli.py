from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .config import ProjectConfigError, load_project_config
from .gates import run_preflight_gate
from .git_state import GitInspectionError, inspect_repo
from .promote import PromotionTransactionError, execute_promotion, prepare_promotion

DEFAULT_MANIFEST = "tools/project_agent_runtime/projects/youmo.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="youmo-promote",
        description=(
            "Promote one published checkpoint to the canonical YouMo branch using an "
            "isolated ff-only merge, full merged-canonical validation, and an exact "
            "remote compare-and-swap. Dry-run unless --execute is supplied."
        ),
    )
    parser.add_argument("--repo", default=".", help="read-only YouMo controller repository")
    parser.add_argument("--project", default=DEFAULT_MANIFEST)
    parser.add_argument("--run-id", required=True, help="published checkpointed YouMo run id")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="perform the validated ff-only canonical promotion; omitted by default",
    )
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

    if not args.execute:
        try:
            plan = prepare_promotion(control_state.root, config, args.run_id)
        except PromotionTransactionError as exc:
            print(f"STOP: promotion preflight failed: {exc}", file=sys.stderr)
            return 14
        print("PROMOTION_PREFLIGHT=PASS")
        print(f"RUN_ID={plan.run_id}")
        print(f"WORKSPACE={plan.workspace}")
        print(f"REPOSITORY={plan.repository}")
        print(f"CANDIDATE_BRANCH={plan.candidate_branch}")
        print(f"CANDIDATE_SHA={plan.candidate_sha}")
        print(f"CANONICAL_BRANCH={plan.canonical_branch}")
        print(f"CANONICAL_SHA={plan.canonical_sha}")
        print(f"CLASSIFICATION={plan.classification}")
        print(f"VALIDATION_VENV={plan.validation_venv or '<missing>'}")
        print("PROMOTION_CLONE=NOT_CREATED")
        print("MERGE=NOT_STARTED")
        print("FULL_MERGED_CANONICAL_VALIDATION=NOT_STARTED")
        print("CANONICAL_PUSH=NOT_STARTED")
        if plan.executable:
            print("MERGE_POLICY=git merge --ff-only")
            print("PUSH_POLICY=exact expected-old-SHA compare-and-swap")
            print(f"RERUN_WITH=youmo-promote --run-id {plan.run_id} --execute")
            return 0
        print("RERUN_WITH=<not-authorized; readiness is not READY_FAST_FORWARD>")
        return 14 if plan.classification == "BLOCKED_DIVERGED" else 0

    try:
        result = execute_promotion(control_state.root, config, args.run_id)
    except PromotionTransactionError as exc:
        print(f"STOP: canonical promotion failed: {exc}", file=sys.stderr)
        return 14

    print(f"PROMOTION_STATUS={result.status}")
    print(f"RUN_ID={result.run_id}")
    print(f"CANDIDATE_BRANCH={result.candidate_branch}")
    print(f"CANDIDATE_SHA={result.candidate_sha}")
    print(f"CANONICAL_BRANCH={result.canonical_branch}")
    print(f"CANONICAL_BEFORE={result.canonical_before}")
    print(f"CANONICAL_AFTER={result.canonical_after}")
    print("MERGE_POLICY=git merge --ff-only")
    print("MERGE_COMMIT_CREATED=FALSE")
    print("FULL_MERGED_CANONICAL_VALIDATION=PASS")
    print("PUSH_POLICY=exact expected-old-SHA compare-and-swap")
    print(f"INTENT={result.intent_path}")
    print(f"EVIDENCE={result.evidence_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
