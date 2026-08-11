from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Sequence

from .architecture import canonical_capsule_json, compile_context_capsule, load_architecture_snapshot
from .codex_transport import CodexTransportError, inspect_codex_sdk, run_codex_turn
from .config import ProjectConfigError, load_project_config
from .gates import GateReport, run_preflight_gate
from .git_state import GitInspectionError, inspect_repo
from .prompting import developer_instructions, plan_prompt
from .state import (
    RuntimeStateError,
    advance_runtime_state,
    load_runtime_state,
    resolve_resume_thread,
    save_runtime_state,
)


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
        help="YouMo project manifest path, relative to repository root",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="show deterministic project state")
    status.add_argument("--json", action="store_true")

    gate = sub.add_parser("gate", help="run fail-closed project preflight")
    gate.add_argument("--json", action="store_true")

    context = sub.add_parser("context", help="emit compact architecture context capsule")
    context.add_argument("--output", help="optional output path")

    doctor = sub.add_parser("doctor", help="inspect Codex SDK readiness without starting Codex")
    doctor.add_argument("--json", action="store_true")

    plan = sub.add_parser("plan", help="ask Codex for a read-only implementation plan")
    plan.add_argument(
        "--execute",
        action="store_true",
        help="actually start a read-only Codex turn; omitted by default for safety",
    )
    plan.add_argument("--fresh-thread", action="store_true", help="do not resume saved Codex thread state")

    build = sub.add_parser("build", help="validate the guarded implementation context")
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
        runtime = load_runtime_state(state.root, config.state_dir, project_id=config.project_id)
        payload = {
            "project": config.project_id,
            "repository": config.repository,
            "branch": state.branch,
            "head": state.head,
            "clean": state.clean,
            "staged_files": list(state.staged_files),
            "preflight": _report_payload(report),
            "runtime": {
                "thread_id": runtime.thread_id,
                "last_turn_id": runtime.last_turn_id,
                "last_turn_status": runtime.last_turn_status,
                "last_head": runtime.last_head,
                "architecture_lock_sha256": runtime.architecture_lock_sha256,
            },
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
            print(f"CODEX_THREAD={runtime.thread_id or '<none>'}")
        return 0 if report.passed else 2

    if args.command == "gate":
        if args.json:
            print(json.dumps(_report_payload(report), indent=2, sort_keys=True))
        else:
            _print_gate(report)
        return 0 if report.passed else 2

    if args.command == "doctor":
        sdk = inspect_codex_sdk(config.codex.sdk_requirement)
        payload = {
            "installed": sdk.installed,
            "compatible": sdk.compatible,
            "ready": sdk.ready,
            "version": sdk.version,
            "requirement": sdk.requirement,
            "detail": sdk.detail,
            "transport_started": False,
        }
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"SDK={'READY' if sdk.ready else ('MISMATCH' if sdk.installed else 'MISSING')}")
            print(f"REQUIREMENT={sdk.requirement}")
            print(f"VERSION={sdk.version or '<none>'}")
            print("CODEX_TRANSPORT=NOT_STARTED")
        return 0 if sdk.ready else 4

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

    if args.command == "plan":
        if not args.execute:
            print("PREFLIGHT=PASS")
            print(f"MODEL={config.codex.implementation_model}")
            print(f"REASONING={config.codex.implementation_reasoning}")
            print("SANDBOX=read_only")
            print("CODEX_TRANSPORT=NOT_STARTED")
            print("RERUN_WITH=youmo plan --execute")
            return 0

        sdk = inspect_codex_sdk(config.codex.sdk_requirement)
        if not sdk.ready:
            print(f"STOP: {sdk.detail}; expected {sdk.requirement}", file=sys.stderr)
            return 4

        runtime = load_runtime_state(state.root, config.state_dir, project_id=config.project_id)
        try:
            saved_thread = resolve_resume_thread(
                runtime,
                current_head=state.head,
                architecture_lock_sha256=snapshot.lock_sha256,
                fresh_thread=args.fresh_thread,
            )
        except RuntimeStateError as exc:
            print(
                f"STOP: {exc}. Use 'youmo plan --execute --fresh-thread' to intentionally start new context.",
                file=sys.stderr,
            )
            return 6
        instructions = developer_instructions(capsule, mode="plan")
        try:
            result = asyncio.run(
                run_codex_turn(
                    repo_root=state.root,
                    prompt=plan_prompt(),
                    developer_instructions=instructions,
                    model=config.codex.implementation_model,
                    reasoning=config.codex.implementation_reasoning,
                    sandbox_name="read_only",
                    thread_id=saved_thread,
                )
            )
        except CodexTransportError as exc:
            print(f"STOP: Codex transport failed: {exc}", file=sys.stderr)
            return 5

        updated = advance_runtime_state(
            runtime,
            thread_id=result.thread_id,
            turn_id=result.turn_id,
            turn_status=result.status,
            head=state.head,
            architecture_lock_sha256=snapshot.lock_sha256,
        )
        save_runtime_state(state.root, config.state_dir, updated)
        print(result.final_response or "")
        return 0 if result.status.lower() in {"completed", "complete", "success", "succeeded"} else 5

    if args.command == "build":
        if args.dry_run:
            print("PREFLIGHT=PASS")
            print(f"CONTEXT_BYTES={len(rendered.encode('utf-8'))}")
            print(f"IMPLEMENTATION_MODEL={config.codex.implementation_model}")
            print(f"IMPLEMENTATION_REASONING={config.codex.implementation_reasoning}")
            print(f"AUDIT_MODEL={config.codex.audit_model}")
            print(f"AUDIT_REASONING={config.codex.audit_reasoning}")
            print("CODEX_TRANSPORT=NOT_STARTED")
            return 0
        print(
            "STOP: write-capable Codex execution remains disabled until the isolated-workspace phase is complete. "
            "Use 'youmo build --dry-run'; 'youmo plan --execute' is read-only.",
            file=sys.stderr,
        )
        return 3

    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
