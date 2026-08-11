from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Sequence

from .architecture import compile_context_capsule, load_architecture_snapshot
from .audit_engine import (
    AuditGuardError,
    load_build_evidence,
    run_guarded_audit,
    verify_audit_preconditions,
)
from .codex_transport import CodexTransportError, inspect_codex_sdk, run_codex_turn
from .config import ProjectConfigError, load_project_config
from .gates import run_preflight_gate
from .git_state import GitInspectionError, inspect_repo
from .operation_lease import OperationLeaseError, operation_lease
from .prompting import developer_instructions
from .state import state_directory
from .validation_env import (
    ValidationEnvironmentError,
    bound_workspace_validation_venv,
    resolve_validation_venv,
)
from .validation_policy import FULL_VALIDATION_COMMANDS
from .workspace import WorkspaceError, verify_executor_workspace

DEFAULT_MANIFEST = "tools/project_agent_runtime/projects/youmo.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="youmo-audit",
        description="Independent Sol/high semantic audit for a guarded YouMo build.",
    )
    parser.add_argument("--repo", default=".", help="read-only YouMo control repository")
    parser.add_argument("--project", default=DEFAULT_MANIFEST)
    parser.add_argument("--workspace", required=True, help="dirty isolated executor clone")
    parser.add_argument("--task", required=True, help="exact task text used by youmo-build")
    parser.add_argument("--evidence", required=True, help="build evidence JSON emitted by youmo-build")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="start the read-only Sol audit turn; omitted by default",
    )
    return parser


def _safe_label(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "audit"


def _save_audit_evidence(control_root: Path, state_dir: str, payload: str, branch: str, head: str) -> Path:
    root = state_directory(control_root, state_dir) / "audit-evidence"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{_safe_label(branch)}-{head[:12]}.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(payload, encoding="utf-8")
    temp.replace(path)
    return path


def _expected_architecture(config: object, snapshot: object) -> dict[str, str]:
    lock_path = getattr(config, "architecture_lock")
    sources = getattr(config, "architecture_sources")
    result = {lock_path: getattr(snapshot, "lock_sha256")}
    source_hashes = getattr(snapshot, "source_sha256")
    result.update({path: source_hashes[path] for path in sources})
    return result


def _validation_commands(use_workspace_venv: bool) -> tuple[tuple[str, ...], ...]:
    if not use_workspace_venv:
        return FULL_VALIDATION_COMMANDS
    return tuple(
        tuple(".venv/bin/python" if part == "python3" else part for part in command)
        for command in FULL_VALIDATION_COMMANDS
    )


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
        print("STOP: control-repository preflight failed.", file=sys.stderr)
        return 2

    workspace = Path(args.workspace).expanduser().resolve()
    try:
        workspace_report = verify_executor_workspace(
            control_state.root, workspace, config, require_clean=False, require_registry=True
        )
    except WorkspaceError as exc:
        print(f"STOP: executor verification failed: {exc}", file=sys.stderr)
        return 7
    if not workspace_report.passed or workspace_report.state is None:
        print("STOP: executor isolation/identity gate failed.", file=sys.stderr)
        for check in workspace_report.checks:
            if not check.passed:
                print(f"FAIL {check.name}: {check.detail}", file=sys.stderr)
        return 7

    try:
        evidence = load_build_evidence(Path(args.evidence).expanduser().resolve())
    except AuditGuardError as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 9

    control_arch = load_architecture_snapshot(control_state.root, config)
    executor_arch = load_architecture_snapshot(workspace, config)
    if (
        control_arch.lock_sha256 != executor_arch.lock_sha256
        or control_arch.source_sha256 != executor_arch.source_sha256
    ):
        print("STOP: executor architecture authority differs from control repository.", file=sys.stderr)
        return 9
    expected_arch = _expected_architecture(config, control_arch)

    try:
        head, branch, files, diff_sha = verify_audit_preconditions(
            workspace, args.task, evidence, expected_arch
        )
        validation_venv = resolve_validation_venv(control_state.root)
    except (AuditGuardError, ValidationEnvironmentError) as exc:
        print(f"STOP: audit evidence preflight failed: {exc}", file=sys.stderr)
        return 9

    capsule = compile_context_capsule(config, workspace_report.state, executor_arch)
    instructions = developer_instructions(capsule, mode="audit")

    if not args.execute:
        print("CONTROL_PREFLIGHT=PASS")
        print("EXECUTOR_ISOLATION=PASS")
        print("BUILD_EVIDENCE_BINDING=PASS")
        print("ARCHITECTURE_BINDING=PASS")
        print(f"WORKSPACE={workspace}")
        print(f"BRANCH={branch}")
        print(f"HEAD={head}")
        print(f"CHANGED_FILES={json.dumps(list(files))}")
        print(f"DIFF_SHA256={diff_sha}")
        print(f"MODEL={config.codex.audit_model}")
        print(f"REASONING={config.codex.audit_reasoning}")
        print("SANDBOX=read_only")
        print("AUDIT_VALIDATION=FULL_REGRESSION")
        print(f"VALIDATION_VENV={validation_venv or '<system-python>'}")
        print("EXECUTOR_LEASE=NOT_ACQUIRED")
        print("CODEX_TRANSPORT=NOT_STARTED")
        print("RERUN_WITH=youmo-audit ... --execute")
        return 0

    sdk = inspect_codex_sdk(config.codex.sdk_requirement)
    if not sdk.ready:
        print(f"STOP: {sdk.detail}; expected {sdk.requirement}", file=sys.stderr)
        return 4

    try:
        with operation_lease(control_state.root, config, workspace, "audit"):
            with bound_workspace_validation_venv(workspace, validation_venv) as bound:
                result = asyncio.run(
                    run_guarded_audit(
                        workspace_root=workspace,
                        task=args.task,
                        build_evidence=evidence,
                        expected_architecture=expected_arch,
                        developer_instructions=instructions,
                        model=config.codex.audit_model,
                        reasoning=config.codex.audit_reasoning,
                        turn_runner=run_codex_turn,
                        validation_commands=_validation_commands(bound),
                    )
                )
    except (
        AuditGuardError,
        CodexTransportError,
        OperationLeaseError,
        ValidationEnvironmentError,
    ) as exc:
        print(f"STOP: guarded audit failed: {exc}", file=sys.stderr)
        return 9

    evidence_path = _save_audit_evidence(
        control_state.root, config.state_dir, result.to_json(), result.branch, result.base_head
    )
    print(f"AUDIT_STATUS={result.status}")
    print(f"VERDICT={result.verdict or '<none>'}")
    print(f"EVIDENCE={evidence_path}")
    print(f"DIFF_SHA256={result.diff_sha256}")
    if result.summary:
        print(f"SUMMARY={result.summary}")
    for finding in result.findings:
        print(f"FINDING={finding.severity}:{finding.path or '<none>'}:{finding.message}")
    for violation in result.violations:
        print(f"VIOLATION={violation}")
    return 0 if result.passed else 9


if __name__ == "__main__":
    raise SystemExit(main())
