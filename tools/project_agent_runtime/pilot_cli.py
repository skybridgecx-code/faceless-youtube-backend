from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .config import ProjectConfigError, load_project_config
from .gates import run_preflight_gate
from .git_state import GitInspectionError, inspect_repo
from .pilot import PilotError, inspect_live_readiness, resolve_remote_branch_sha
from .workspace import WorkspaceError, initialize_executor_workspace

DEFAULT_MANIFEST = "tools/project_agent_runtime/projects/youmo.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="youmo-pilot",
        description="Provision and verify an isolated executor before any live Codex write.",
    )
    parser.add_argument("--repo", default=".", help="read-only YouMo controller repository")
    parser.add_argument("--project", default=DEFAULT_MANIFEST)
    sub = parser.add_subparsers(dest="command", required=True)

    resolve = sub.add_parser("resolve", help="resolve a remote branch to an exact SHA")
    resolve.add_argument("--base-ref", help="remote branch (defaults to canonical branch)")

    prepare = sub.add_parser("prepare", help="prepare an isolated executor clone")
    prepare.add_argument("--workspace", required=True)
    prepare.add_argument("--branch", required=True)
    prepare.add_argument("--base-ref", help="remote branch (defaults to canonical branch)")
    prepare.add_argument("--execute", action="store_true")

    ready = sub.add_parser("ready", help="run the complete read-only live-build readiness gate")
    ready.add_argument("--workspace", required=True)
    ready.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        state = inspect_repo(Path(args.repo).resolve())
        config = load_project_config(state.root, args.project)
    except (GitInspectionError, ProjectConfigError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    preflight = run_preflight_gate(state.root, config, state)
    if not preflight.passed:
        print("STOP: controller repository preflight failed.", file=sys.stderr)
        for check in preflight.checks:
            if not check.passed:
                print(f"FAIL {check.name}: {check.detail}", file=sys.stderr)
        return 2

    if args.command == "ready":
        report = inspect_live_readiness(
            state.root,
            config,
            state,
            Path(args.workspace),
        )
        if args.json:
            print(report.to_json(), end="")
        else:
            print(f"READY_FOR_LIVE_BUILD={'YES' if report.ready else 'NO'}")
            print(f"WORKSPACE={report.workspace}")
            for check in report.checks:
                print(
                    f"CHECK={check.name}:{'PASS' if check.passed else 'FAIL'}:{check.detail}"
                )
            for warning in report.warnings:
                print(f"WARNING={warning}")
            print("CODEX_TRANSPORT=NOT_STARTED")
            print("MUTATIONS=NONE")
        return 0 if report.ready else 12

    base_ref = (args.base_ref or config.canonical_branch).strip()
    try:
        base_sha = resolve_remote_branch_sha(state.root, base_ref)
    except PilotError as exc:
        print(f"STOP: remote base resolution failed: {exc}", file=sys.stderr)
        return 12

    if args.command == "resolve":
        print(f"BASE_REF={base_ref}")
        print(f"BASE_SHA={base_sha}")
        print("CONTROL_REPO_REFS_MUTATED=NO")
        print("WORKTREE_MUTATED=NO")
        return 0

    workspace = Path(args.workspace).expanduser().resolve()
    if workspace.exists():
        print(f"STOP: executor destination already exists: {workspace}", file=sys.stderr)
        return 12

    if not args.execute:
        print("PILOT_PREPARE=DRY_RUN")
        print(f"WORKSPACE={workspace}")
        print(f"EXECUTION_BRANCH={args.branch}")
        print(f"BASE_REF={base_ref}")
        print(f"BASE_SHA={base_sha}")
        print("ACTIVE_PROJECT_CHECKOUT_MUTATION=NONE")
        print("CONTROL_REPO_REFS_MUTATED=NO")
        print("CODEX_TRANSPORT=NOT_STARTED")
        print("RERUN_WITH=youmo-pilot prepare ... --execute")
        return 0

    try:
        report = initialize_executor_workspace(
            state.root,
            workspace,
            config,
            base_sha=base_sha,
            branch=args.branch,
        )
    except WorkspaceError as exc:
        print(f"STOP: executor preparation failed: {exc}", file=sys.stderr)
        return 12

    if not report.passed or report.state is None:
        print("STOP: executor post-create verification failed.", file=sys.stderr)
        for check in report.checks:
            if not check.passed:
                print(f"FAIL {check.name}: {check.detail}", file=sys.stderr)
        return 12

    print("PILOT_PREPARE=COMPLETE")
    print(f"WORKSPACE={workspace}")
    print(f"EXECUTION_BRANCH={report.state.branch}")
    print(f"BASE_REF={base_ref}")
    print(f"BASE_SHA={report.state.head}")
    print("ACTIVE_PROJECT_CHECKOUT_MUTATION=NONE")
    print("CONTROL_REPO_REFS_MUTATED=NO")
    print("CODEX_TRANSPORT=NOT_STARTED")
    print(f"NEXT_COMMAND=youmo-pilot ready --workspace {workspace}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
