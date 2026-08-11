from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Sequence

from .architecture import load_architecture_snapshot
from .audit_engine import AuditGuardError, load_build_evidence, verify_audit_preconditions
from .checkpoint_engine import (
    CheckpointError,
    create_checkpoint,
    load_audit_evidence,
    sha256_file,
)
from .config import ProjectConfigError, load_project_config
from .gates import run_preflight_gate
from .git_state import GitInspectionError, inspect_repo
from .hygiene import WorkspaceHygieneError, plan_ignored_cleanup
from .operation_lease import OperationLeaseError, operation_lease
from .state import state_directory
from .workspace import WorkspaceError, verify_executor_workspace

DEFAULT_MANIFEST = "tools/project_agent_runtime/projects/youmo.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="youmo-checkpoint",
        description="Turn an audited YouMo executor diff into one local checkpoint commit.",
    )
    parser.add_argument("--repo", default=".", help="read-only YouMo control repository")
    parser.add_argument("--project", default=DEFAULT_MANIFEST)
    parser.add_argument("--workspace", required=True, help="dirty isolated executor clone")
    parser.add_argument("--task", required=True, help="exact task text used for build/audit")
    parser.add_argument("--build-evidence", required=True)
    parser.add_argument("--audit-evidence", required=True)
    parser.add_argument("--subject", default="YouMo audited checkpoint")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="create the local checkpoint commit; omitted by default",
    )
    return parser


def _safe_label(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "checkpoint"


def _expected_architecture(config: object, snapshot: object) -> dict[str, str]:
    lock_path = getattr(config, "architecture_lock")
    sources = getattr(config, "architecture_sources")
    result = {lock_path: getattr(snapshot, "lock_sha256")}
    source_hashes = getattr(snapshot, "source_sha256")
    result.update({path: source_hashes[path] for path in sources})
    return result


def _save_evidence(control_root: Path, state_dir: str, payload: str, branch: str, commit_sha: str) -> Path:
    root = state_directory(control_root, state_dir) / "checkpoint-evidence"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{_safe_label(branch)}-{commit_sha[:12]}.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(payload, encoding="utf-8")
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

    build_path = Path(args.build_evidence).expanduser().resolve()
    audit_path = Path(args.audit_evidence).expanduser().resolve()
    try:
        build = load_build_evidence(build_path)
        audit = load_audit_evidence(audit_path)
    except (AuditGuardError, CheckpointError) as exc:
        print(f"STOP: evidence load failed: {exc}", file=sys.stderr)
        return 10

    for key in ("base_head", "branch", "task_sha256", "diff_sha256"):
        if audit.get(key) != build.get(key):
            print(f"STOP: build/audit evidence mismatch for {key}", file=sys.stderr)
            return 10

    control_arch = load_architecture_snapshot(control_state.root, config)
    executor_arch = load_architecture_snapshot(workspace, config)
    if (
        control_arch.lock_sha256 != executor_arch.lock_sha256
        or control_arch.source_sha256 != executor_arch.source_sha256
    ):
        print("STOP: executor architecture authority differs from control repository.", file=sys.stderr)
        return 10
    expected_arch = _expected_architecture(config, control_arch)

    try:
        head, branch, files, diff_sha = verify_audit_preconditions(
            workspace, args.task, build, expected_arch
        )
        ignored = plan_ignored_cleanup(workspace)
    except (AuditGuardError, WorkspaceHygieneError) as exc:
        print(f"STOP: checkpoint preflight failed: {exc}", file=sys.stderr)
        return 10
    if head != audit["base_head"] or branch != audit["branch"] or diff_sha != audit["diff_sha256"]:
        print("STOP: audit evidence no longer binds current executor state.", file=sys.stderr)
        return 10

    if not args.execute:
        print("CONTROL_PREFLIGHT=PASS")
        print("EXECUTOR_ISOLATION=PASS")
        print("BUILD_AUDIT_BINDING=PASS")
        print("CURRENT_DIFF_BINDING=PASS")
        print(f"WORKSPACE={workspace}")
        print(f"BRANCH={branch}")
        print(f"PARENT_HEAD={head}")
        print(f"CHANGED_FILES={json.dumps(list(files))}")
        print(f"DIFF_SHA256={diff_sha}")
        print(f"IGNORED_ARTIFACTS_TO_CLEAN={json.dumps(list(ignored))}")
        print("EXECUTOR_LEASE=NOT_ACQUIRED")
        print("GIT_COMMIT=NOT_CREATED")
        print("GIT_PUSH=NOT_STARTED")
        print("RERUN_WITH=youmo-checkpoint ... --execute")
        return 0

    try:
        with operation_lease(control_state.root, config, workspace, "checkpoint"):
            result = create_checkpoint(
                control_root=control_state.root,
                workspace_root=workspace,
                config=config,
                task=args.task,
                build_evidence=build,
                audit_evidence=audit,
                expected_architecture=expected_arch,
                build_evidence_sha256=sha256_file(build_path),
                audit_evidence_sha256=sha256_file(audit_path),
                subject=args.subject,
            )
    except (CheckpointError, OperationLeaseError) as exc:
        print(f"STOP: checkpoint failed: {exc}", file=sys.stderr)
        return 10

    evidence_path = _save_evidence(
        control_state.root, config.state_dir, result.to_json(), result.branch, result.commit_sha
    )
    print(f"CHECKPOINT_STATUS={result.status}")
    print(f"COMMIT_SHA={result.commit_sha}")
    print(f"PARENT_SHA={result.parent_sha}")
    print(f"BRANCH={result.branch}")
    print(f"EVIDENCE={evidence_path}")
    print(f"CHANGED_FILES={json.dumps(list(result.changed_files))}")
    print(f"REMOVED_IGNORED_ARTIFACTS={json.dumps(list(result.removed_ignored_artifacts))}")
    print("GIT_PUSH=NOT_STARTED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
