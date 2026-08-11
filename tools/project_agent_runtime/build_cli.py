from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Sequence

from .architecture import compile_context_capsule, load_architecture_snapshot
from .build_engine import BuildGuardError
from .codex_transport import CodexTransportError, inspect_codex_sdk, run_codex_turn
from .config import ProjectConfigError, load_project_config
from .gates import run_preflight_gate
from .git_state import GitInspectionError, inspect_repo
from .operation_lease import OperationLeaseError, operation_lease
from .prompting import developer_instructions
from .safe_build import run_fast_guarded_build
from .state import state_directory
from .validation_env import ValidationEnvironmentError, resolve_validation_venv
from .workspace import WorkspaceError, verify_executor_workspace

DEFAULT_MANIFEST = "tools/project_agent_runtime/projects/youmo.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="youmo-build",
        description="Guarded write-capable Codex build turn for an isolated YouMo executor clone.",
    )
    parser.add_argument("--repo", default=".", help="read-only YouMo control repository")
    parser.add_argument("--project", default=DEFAULT_MANIFEST)
    parser.add_argument("--workspace", required=True, help="physically independent executor clone")
    parser.add_argument("--task", required=True, help="exact implementation objective")
    parser.add_argument(
        "--allow-path",
        dest="allowed_paths",
        action="append",
        required=True,
        help="exclusive write scope; repeat for multiple paths",
    )
    parser.add_argument("--max-changed-files", type=int, default=20)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="start the workspace-write Codex turn; omitted by default",
    )
    return parser


def _safe_label(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "build"


def _save_evidence(control_root: Path, state_dir: str, result_json: str, branch: str, head: str) -> Path:
    root = state_directory(control_root, state_dir) / "build-evidence"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{_safe_label(branch)}-{head[:12]}.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(result_json, encoding="utf-8")
    temp.replace(path)
    return path


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
        for check in preflight.checks:
            if not check.passed:
                print(f"FAIL {check.name}: {check.detail}", file=sys.stderr)
        return 2

    workspace = Path(args.workspace).expanduser().resolve()
    try:
        workspace_report = verify_executor_workspace(
            control_state.root, workspace, config, require_clean=True, require_registry=True
        )
    except WorkspaceError as exc:
        print(f"STOP: executor verification failed: {exc}", file=sys.stderr)
        return 7
    if not workspace_report.passed or workspace_report.state is None:
        print("STOP: executor isolation gate failed.", file=sys.stderr)
        for check in workspace_report.checks:
            if not check.passed:
                print(f"FAIL {check.name}: {check.detail}", file=sys.stderr)
        return 7

    control_arch = load_architecture_snapshot(control_state.root, config)
    executor_arch = load_architecture_snapshot(workspace, config)
    if (
        control_arch.lock_sha256 != executor_arch.lock_sha256
        or control_arch.source_sha256 != executor_arch.source_sha256
    ):
        print("STOP: executor architecture authority differs from control repository.", file=sys.stderr)
        return 7

    executor_state = workspace_report.state
    capsule = compile_context_capsule(config, executor_state, executor_arch)
    instructions = developer_instructions(capsule, mode="implement")

    try:
        validation_venv = resolve_validation_venv(control_state.root)
    except ValidationEnvironmentError as exc:
        print(f"STOP: validation environment failed: {exc}", file=sys.stderr)
        return 6

    if not args.execute:
        print("CONTROL_PREFLIGHT=PASS")
        print("EXECUTOR_ISOLATION=PASS")
        print("ARCHITECTURE_BINDING=PASS")
        print(f"WORKSPACE={workspace}")
        print(f"BRANCH={executor_state.branch}")
        print(f"HEAD={executor_state.head}")
        print(f"MODEL={config.codex.implementation_model}")
        print(f"REASONING={config.codex.implementation_reasoning}")
        print("SANDBOX=workspace_write")
        print(f"ALLOW_PATHS={json.dumps(args.allowed_paths)}")
        print(f"MAX_CHANGED_FILES={args.max_changed_files}")
        print("BUILD_VALIDATION=TARGETED_FAST")
        print("AUDIT_VALIDATION=FULL_REGRESSION")
        print(f"VALIDATION_VENV={validation_venv or '<system-python>'}")
        print("EXECUTOR_LEASE=NOT_ACQUIRED")
        print("CODEX_TRANSPORT=NOT_STARTED")
        print("RERUN_WITH=youmo-build ... --execute")
        return 0

    sdk = inspect_codex_sdk(config.codex.sdk_requirement)
    if not sdk.ready:
        print(f"STOP: {sdk.detail}; expected {sdk.requirement}", file=sys.stderr)
        return 4

    try:
        with operation_lease(control_state.root, config, workspace, "build"):
            result = asyncio.run(
                run_fast_guarded_build(
                    workspace_root=workspace,
                    task=args.task,
                    allowed_paths=args.allowed_paths,
                    architecture_paths=(config.architecture_lock, *config.architecture_sources),
                    developer_instructions=instructions,
                    model=config.codex.implementation_model,
                    reasoning=config.codex.implementation_reasoning,
                    turn_runner=run_codex_turn,
                    max_changed_files=args.max_changed_files,
                    validation_venv=validation_venv,
                )
            )
    except (BuildGuardError, CodexTransportError, OperationLeaseError, ValidationEnvironmentError) as exc:
        print(f"STOP: guarded build failed: {exc}", file=sys.stderr)
        return 8

    evidence = _save_evidence(
        control_state.root,
        config.state_dir,
        result.to_json(),
        result.branch,
        result.base_head,
    )
    print(f"BUILD_STATUS={result.status}")
    print(f"EVIDENCE={evidence}")
    print(f"CHANGED_FILES={json.dumps(list(result.changed_files))}")
    print(f"DIFF_SHA256={result.diff_sha256 or '<none>'}")
    print("BUILD_VALIDATION=TARGETED_FAST")
    print("NEXT_REQUIRED_GATE=FULL_AUDIT")
    if result.violations:
        for violation in result.violations:
            print(f"VIOLATION={violation}")
    return 0 if result.ready_for_audit else 8


if __name__ == "__main__":
    raise SystemExit(main())
