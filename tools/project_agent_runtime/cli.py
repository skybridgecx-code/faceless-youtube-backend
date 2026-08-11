from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .architecture import canonical_capsule_json, compile_context_capsule, load_architecture_snapshot
from .config import ProjectConfigError, load_project_config
from .gates import GateReport, run_preflight_gate
from .git_state import GitInspectionError, inspect_repo


DEFAULT_MANIFEST = "tools/project_agent_runtime/projects/youmo.json"


def _report_payload(report: GateReport) -> dict[str, object]:
    return {
        "passed": report.passed,
        "checks": [
            {"name": check.name, "passed": check.passed, "detail": check.detail}
            for check in report.checks
        ],
    }


def _print_gate(report: GateReport) -> None:
    for check in report.checks:
        marker = "PASS" if check.passed else "FAIL"
        print(f"[{marker}] {check.name}: {check.detail}")
    print(f"GATE={'PASS' if report.passed else 'FAIL'}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="youmo",
        description="Project-scoped engineering runtime for YouMo.",
    )
    parser.add_argument("--repo", default=".", help="repository root or child path")
    parser.add_argument(
        "--project",
        default=DEFAULT_MANIFEST,
        help="project manifest path, relative to repository root",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="show deterministic project state")
    status.add_argument("--json", action="store_true")

    gate = sub.add_parser("gate", help="run fail-closed project preflight")
    gate.add_argument("--json", action="store_true")

    context = sub.add_parser("context", help="emit compact architecture context capsule")
    context.add_argument("--output", help="optional output path")

    build = sub.add_parser("build", help="run guarded Codex build loop")
    build.add_argument(
        "--dry-run",
        action="store_true",
        help="validate preflight/context without starting Codex",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_hint = Path(args.repo).resolve()

    try:
        state = inspect_repo(repo_hint)
        config = load_project_config(state.root, args.project)
    except (GitInspectionError, ProjectConfigError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    report = run_preflight_gate(state.root, config, state)

    if args.command == "status":
        payload = {
            "project": config.project_id,
            "repository": config.repository,
            "branch": state.branch,
            "head": state.head,
            "clean": state.clean,
            "staged_files": list(state.staged_files),
            "preflight": _report_payload(report),
        }
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"PROJECT={config.project_id}")
            print(f"REPOSITORY={config.repository}")
            print(f"BRANCH={state.branch or '<detached>'}")
            print(f"HEAD={state.head}")
            print(f"WORKTREE={'CLEAN' if state.clean else 'DIRTY'}")
            print(f"PREFLIGHT={'PASS' if report.passed else 'FAIL'}")
        return 0 if report.passed else 2

    if args.command == "gate":
        if args.json:
            print(json.dumps(_report_payload(report), indent=2, sort_keys=True))
        else:
            _print_gate(report)
        return 0 if report.passed else 2

    if not report.passed:
        _print_gate(report)
        print("STOP: preflight failed; no agent execution is authorized.", file=sys.stderr)
        return 2

    snapshot = load_architecture_snapshot(state.root, config)
    capsule = compile_context_capsule(config, state, snapshot)
    rendered = canonical_capsule_json(capsule)

    if args.command == "context":
        if args.output:
            output = Path(args.output)
            if not output.is_absolute():
                output = state.root / output
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(rendered, encoding="utf-8")
            print(output)
        else:
            print(rendered, end="")
        return 0

    if args.command == "build":
        if args.dry_run:
            print("PREFLIGHT=PASS")
            print(f"CONTEXT_BYTES={len(rendered.encode('utf-8'))}")
            print("CODEX_TRANSPORT=NOT_STARTED")
            return 0
        print(
            "STOP: Codex transport is intentionally disabled in I0. "
            "Use 'youmo build --dry-run' until the isolated transport phase is audited.",
            file=sys.stderr,
        )
        return 3

    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
