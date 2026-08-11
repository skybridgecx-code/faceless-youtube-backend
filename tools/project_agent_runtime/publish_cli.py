from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .config import ProjectConfigError, load_project_config
from .gates import run_preflight_gate
from .git_state import GitInspectionError, inspect_repo
from .publish import PublishError, execute_publish, prepare_publish

DEFAULT_MANIFEST = "tools/project_agent_runtime/projects/youmo.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="youmo-publish",
        description="Publish one audited YouMo checkpoint to its noncanonical remote branch.",
    )
    parser.add_argument("--repo", default=".", help="read-only YouMo controller repository")
    parser.add_argument("--project", default=DEFAULT_MANIFEST)
    parser.add_argument("--run-id", required=True, help="checkpointed immutable YouMo run id")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="perform the guarded remote branch publication; omitted by default",
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
            plan = prepare_publish(control_state.root, config, args.run_id, allow_fetch=False)
        except PublishError as exc:
            print(f"STOP: publish preflight failed: {exc}", file=sys.stderr)
            return 12
        print("PUBLISH_PREFLIGHT=PASS")
        print(f"RUN_ID={plan.run_id}")
        print(f"WORKSPACE={plan.workspace}")
        print(f"BRANCH={plan.branch}")
        print(f"COMMIT_SHA={plan.commit_sha}")
        print(f"REPOSITORY={plan.repository}")
        print(f"REMOTE_BEFORE={plan.remote_before or '<absent>'}")
        print(f"MODE={plan.mode}")
        print("CANONICAL_BRANCH_MUTATION=FORBIDDEN")
        print("MERGE=NOT_STARTED")
        print("GIT_PUSH=NOT_STARTED")
        if plan.already_published:
            print("RERUN_WITH=<not-needed; remote already equals checkpoint>")
        else:
            print(f"RERUN_WITH=youmo-publish --run-id {plan.run_id} --execute")
        return 0

    try:
        result = execute_publish(control_state.root, config, args.run_id)
    except PublishError as exc:
        print(f"STOP: publish failed: {exc}", file=sys.stderr)
        return 12

    print(f"PUBLISH_STATUS={result.status}")
    print(f"RUN_ID={result.run_id}")
    print(f"WORKSPACE={result.workspace}")
    print(f"BRANCH={result.branch}")
    print(f"COMMIT_SHA={result.commit_sha}")
    print(f"REPOSITORY={result.repository}")
    print(f"REMOTE_BEFORE={result.remote_before or '<absent>'}")
    print(f"REMOTE_AFTER={result.remote_after}")
    print(f"MODE={result.mode}")
    print(f"EVIDENCE={result.evidence_path}")
    print("CANONICAL_BRANCH_MUTATED=FALSE")
    print("MERGE=NOT_STARTED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
