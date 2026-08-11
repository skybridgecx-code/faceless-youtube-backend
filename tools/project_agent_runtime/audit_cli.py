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
from .config import ProjectConfig, ProjectConfigError, load_project_config
from .gates import run_preflight_gate
from .git_state import GitInspectionError, inspect_repo
from .operation_lease import OperationLeaseError, operation_lease
from .prompting import developer_instructions
from .run_manifest import (
    RunManifest,
    RunManifestError,
    load_run_manifest,
    resolve_evidence,
    run_directory,
    transition_run,
    write_run_evidence,
)
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
    parser.add_argument("--run-id", help="immutable YouMo run identifier")
    parser.add_argument("--workspace", help="dirty isolated executor clone (legacy/manual mode)")
    parser.add_argument("--task", help="exact task text (legacy/manual mode)")
    parser.add_argument("--evidence", help="build evidence JSON (legacy/manual mode)")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="start the read-only Sol audit turn; omitted by default",
    )
    return parser


def _safe_label(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "audit"


def _save_legacy_audit_evidence(
    control_root: Path, state_dir: str, payload: str, branch: str, head: str
) -> Path:
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


def _resolve_run_inputs(
    control_root: Path,
    config: ProjectConfig,
    args: argparse.Namespace,
) -> tuple[RunManifest | None, Path, str, Path]:
    if args.run_id:
        manifest = load_run_manifest(control_root, config, args.run_id)
        if manifest.stage != "READY_FOR_AUDIT":
            raise RunManifestError(
                f"run {manifest.run_id} is at stage {manifest.stage}, not READY_FOR_AUDIT"
            )
        if manifest.build_evidence is None:
            raise RunManifestError("run has no bound build evidence")
        workspace = Path(manifest.workspace).resolve()
        if args.workspace and Path(args.workspace).expanduser().resolve() != workspace:
            raise RunManifestError("--workspace does not match run manifest workspace")
        if args.task is not None and args.task.strip() != manifest.task:
            raise RunManifestError("--task does not match run manifest task")
        evidence = resolve_evidence(
            manifest.build_evidence,
            expected_parent=run_directory(control_root, config, manifest.run_id),
        )
        if args.evidence and Path(args.evidence).expanduser().resolve() != evidence:
            raise RunManifestError("--evidence does not match run manifest build evidence")
        return manifest, workspace, manifest.task, evidence

    missing = [name for name in ("workspace", "task", "evidence") if not getattr(args, name)]
    if missing:
        raise RunManifestError(
            "manual audit mode requires " + ", ".join(f"--{name}" for name in missing)
        )
    task = args.task.strip()
    if not task:
        raise RunManifestError("--task must contain non-whitespace content")
    return (
        None,
        Path(args.workspace).expanduser().resolve(),
        task,
        Path(args.evidence).expanduser().resolve(),
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

    try:
        manifest, workspace, task, build_path = _resolve_run_inputs(
            control_state.root, config, args
        )
    except RunManifestError as exc:
        print(f"STOP: run binding failed: {exc}", file=sys.stderr)
        return 9

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
        evidence = load_build_evidence(build_path)
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
    if manifest is not None and control_arch.lock_sha256 != manifest.architecture_lock_sha256:
        print("STOP: run architecture lock no longer matches controller authority.", file=sys.stderr)
        return 9
    expected_arch = _expected_architecture(config, control_arch)

    try:
        head, branch, files, diff_sha = verify_audit_preconditions(
            workspace, task, evidence, expected_arch
        )
        validation_venv = resolve_validation_venv(control_state.root)
    except (AuditGuardError, ValidationEnvironmentError) as exc:
        print(f"STOP: audit evidence preflight failed: {exc}", file=sys.stderr)
        return 9

    if manifest is not None:
        if head != manifest.base_head or branch != manifest.branch:
            print("STOP: executor identity no longer matches run manifest.", file=sys.stderr)
            return 9

    capsule = compile_context_capsule(config, workspace_report.state, executor_arch)
    instructions = developer_instructions(capsule, mode="audit")

    if not args.execute:
        print("CONTROL_PREFLIGHT=PASS")
        print("EXECUTOR_ISOLATION=PASS")
        print("BUILD_EVIDENCE_BINDING=PASS")
        print("ARCHITECTURE_BINDING=PASS")
        if manifest is not None:
            print(f"RUN_ID={manifest.run_id}")
            print(f"RUN_STAGE={manifest.stage}")
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
        print(
            f"RERUN_WITH=youmo-audit --run-id {manifest.run_id} --execute"
            if manifest is not None
            else "RERUN_WITH=youmo-audit ... --execute"
        )
        return 0

    sdk = inspect_codex_sdk(config.codex.sdk_requirement)
    if not sdk.ready:
        print(f"STOP: {sdk.detail}; expected {sdk.requirement}", file=sys.stderr)
        return 4

    running_manifest = manifest
    if manifest is not None:
        try:
            running_manifest = transition_run(
                control_state.root,
                config,
                manifest,
                new_stage="AUDIT_RUNNING",
                last_error=None,
            )
        except RunManifestError as exc:
            print(f"STOP: run transition failed: {exc}", file=sys.stderr)
            return 9

    try:
        with operation_lease(control_state.root, config, workspace, "audit"):
            with bound_workspace_validation_venv(workspace, validation_venv) as bound:
                result = asyncio.run(
                    run_guarded_audit(
                        workspace_root=workspace,
                        task=task,
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
        if running_manifest is not None:
            try:
                transition_run(
                    control_state.root,
                    config,
                    running_manifest,
                    new_stage="READY_FOR_AUDIT",
                    last_error=str(exc),
                )
            except RunManifestError:
                pass
            print(f"RUN_ID={running_manifest.run_id}")
        print(f"STOP: guarded audit failed: {exc}", file=sys.stderr)
        return 9

    if running_manifest is not None:
        try:
            audit_binding = write_run_evidence(
                control_state.root,
                config,
                running_manifest,
                stage_name="audit",
                payload=result.to_json(),
            )
            final_manifest = transition_run(
                control_state.root,
                config,
                running_manifest,
                new_stage="READY_FOR_CHECKPOINT" if result.passed else "AUDIT_FAILED",
                audit_evidence=audit_binding,
                last_error=None if result.passed else result.summary or result.status,
            )
            evidence_path = audit_binding.path
        except RunManifestError as exc:
            print(f"RUN_ID={running_manifest.run_id}")
            print(f"STOP: audit evidence persistence failed: {exc}", file=sys.stderr)
            return 9
    else:
        final_manifest = None
        evidence_path = str(
            _save_legacy_audit_evidence(
                control_state.root, config.state_dir, result.to_json(), result.branch, result.base_head
            )
        )

    if final_manifest is not None:
        print(f"RUN_ID={final_manifest.run_id}")
        print(f"RUN_STAGE={final_manifest.stage}")
    print(f"AUDIT_STATUS={result.status}")
    print(f"VERDICT={result.verdict or '<none>'}")
    print(f"EVIDENCE={evidence_path}")
    print(f"DIFF_SHA256={result.diff_sha256}")
    if final_manifest is not None and result.passed:
        print(f"NEXT_COMMAND=youmo-resume --run-id {final_manifest.run_id}")
    if result.summary:
        print(f"SUMMARY={result.summary}")
    for finding in result.findings:
        print(f"FINDING={finding.severity}:{finding.path or '<none>'}:{finding.message}")
    for violation in result.violations:
        print(f"VIOLATION={violation}")
    return 0 if result.passed else 9


if __name__ == "__main__":
    raise SystemExit(main())
