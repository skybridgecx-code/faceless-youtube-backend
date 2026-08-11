from __future__ import annotations

import argparse
import contextlib
import io
import re
import sys
from pathlib import Path
from typing import Callable, Sequence

from .build_cli import main as build_main
from .config import ProjectConfigError, load_project_config
from .doctor import diagnose_workspace
from .gates import run_preflight_gate
from .git_state import GitInspectionError, inspect_repo
from .promotion_cli import main as promotion_check_main
from .publish_cli import main as publish_main
from .resume_cli import main as resume_main
from .run_manifest import RunManifest, RunManifestError, load_run_manifest

DEFAULT_MANIFEST = "tools/project_agent_runtime/projects/youmo.json"
_TARGETS = ("build", "audit", "checkpoint", "publish", "promotion-check")
_TARGET_RANK = {name: index for index, name in enumerate(_TARGETS, start=1)}
_RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class FlowError(RuntimeError):
    """Raised when the bounded YouMo flow cannot advance safely."""


ChildMain = Callable[[Sequence[str] | None], int]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="youmo-flow",
        description=(
            "Advance one YouMo job through the existing guarded build, audit, checkpoint, "
            "noncanonical publish, and promotion-readiness gates. Canonical promotion is "
            "never executed by this command."
        ),
    )
    parser.add_argument("--repo", default=".", help="read-only YouMo controller repository")
    parser.add_argument("--project", default=DEFAULT_MANIFEST)
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--run-id", help="resume an existing immutable YouMo run")
    identity.add_argument("--workspace", help="start a new run in this isolated executor clone")
    parser.add_argument("--task", help="new-run implementation objective")
    parser.add_argument(
        "--allow-path",
        dest="allowed_paths",
        action="append",
        default=[],
        help="new-run exclusive write scope; repeat for multiple paths",
    )
    parser.add_argument("--max-changed-files", type=int, default=20)
    parser.add_argument("--subject", default="YouMo audited checkpoint")
    parser.add_argument(
        "--until",
        choices=_TARGETS,
        default="promotion-check",
        help="last safe gate to reach (default: promotion-check)",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=5,
        help="hard cap on mutating/inspection gates executed in this invocation (1-5)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="advance the bounded safe flow; omitted by default",
    )
    return parser


def _validate_arguments(args: argparse.Namespace) -> None:
    if args.max_steps < 1 or args.max_steps > 5:
        raise FlowError("--max-steps must be between 1 and 5")
    if args.max_changed_files < 1:
        raise FlowError("--max-changed-files must be positive")
    if args.run_id:
        if not _RUN_ID_RE.fullmatch(args.run_id):
            raise FlowError("--run-id must be a 32-character lowercase hexadecimal identifier")
        if args.task is not None or args.allowed_paths:
            raise FlowError("--task/--allow-path are only valid when starting from --workspace")
    else:
        task = (args.task or "").strip()
        if not task:
            raise FlowError("--task is required when starting from --workspace")
        if not args.allowed_paths:
            raise FlowError("at least one --allow-path is required when starting from --workspace")
        if any(not value.strip() for value in args.allowed_paths):
            raise FlowError("--allow-path values must be non-empty")


def _invoke(label: str, main_func: ChildMain, argv: Sequence[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        rc = main_func(list(argv))
    out = stdout.getvalue()
    err = stderr.getvalue()
    print(f"FLOW_STEP={label}")
    if out:
        print(out, end="" if out.endswith("\n") else "\n")
    if err:
        print(err, end="" if err.endswith("\n") else "\n", file=sys.stderr)
    print(f"FLOW_STEP_RESULT={rc}")
    return rc, out, err


def _run_id_from_output(output: str) -> str:
    values = []
    for line in output.splitlines():
        if line.startswith("RUN_ID="):
            value = line.split("=", 1)[1].strip()
            if _RUN_ID_RE.fullmatch(value):
                values.append(value)
    unique = tuple(dict.fromkeys(values))
    if len(unique) != 1:
        raise FlowError(
            f"guarded build did not emit exactly one valid RUN_ID (observed={list(unique)!r})"
        )
    return unique[0]


def _stage_rank(manifest: RunManifest) -> int:
    if manifest.stage == "CHECKPOINTED":
        return _TARGET_RANK["checkpoint"]
    if manifest.stage in {"READY_FOR_CHECKPOINT", "CHECKPOINT_RUNNING", "CHECKPOINT_FAILED"}:
        return _TARGET_RANK["audit"]
    if manifest.stage in {"READY_FOR_AUDIT", "AUDIT_RUNNING", "AUDIT_FAILED"}:
        return _TARGET_RANK["build"]
    return 0


def _target_satisfied_by_manifest(manifest: RunManifest, target: str) -> bool:
    if target in {"publish", "promotion-check"}:
        return False
    return _stage_rank(manifest) >= _TARGET_RANK[target]


def _print_boundary(run_id: str, target: str, steps: int, *, detail: str) -> None:
    print(f"FLOW_RUN_ID={run_id}")
    print(f"FLOW_TARGET={target}")
    print(f"FLOW_STEPS_EXECUTED={steps}")
    print(f"FLOW_DETAIL={detail}")
    print("CANONICAL_PROMOTION=NEVER_AUTOMATIC")
    print("YOUMO_PROMOTE_EXECUTE=NOT_STARTED")


def _dry_run_new(
    control_root: Path,
    project: str,
    args: argparse.Namespace,
) -> int:
    build_argv = [
        "--repo",
        str(control_root),
        "--project",
        project,
        "--workspace",
        args.workspace,
        "--task",
        args.task.strip(),
        "--max-changed-files",
        str(args.max_changed_files),
    ]
    for path in args.allowed_paths:
        build_argv.extend(["--allow-path", path])
    rc, _, _ = _invoke("BUILD_PREFLIGHT", build_main, build_argv)
    if rc != 0:
        return rc
    target_index = _TARGET_RANK[args.until]
    planned = list(_TARGETS[:target_index])
    print(f"FLOW_MODE=DRY_RUN")
    print(f"FLOW_PLAN={'>'.join(name.upper().replace('-', '_') for name in planned)}")
    print(f"FLOW_MAX_STEPS={args.max_steps}")
    print("RUN_MANIFEST=NOT_CREATED")
    print("CODEX_TRANSPORT=NOT_STARTED")
    print("NONCANONICAL_PUBLISH=NOT_STARTED")
    print("CANONICAL_PROMOTION=NEVER_AUTOMATIC")
    print("YOUMO_PROMOTE_EXECUTE=NOT_STARTED")
    print("RERUN_WITH=youmo-flow ... --execute")
    return 0


def _safe_existing_state(
    control_root: Path,
    config: object,
    manifest: RunManifest,
) -> str:
    diagnosis = diagnose_workspace(control_root, config, Path(manifest.workspace))
    if diagnosis.run_id != manifest.run_id and diagnosis.state != "LEASE_ACTIVE":
        raise FlowError(
            "requested run is not the latest bound run for its executor; flow will not bypass a newer run"
        )
    safe = {
        "READY_FOR_AUDIT",
        "READY_FOR_CHECKPOINT",
        "CHECKPOINT_RETRYABLE",
        "CHECKPOINTED_CLEAN",
    }
    if diagnosis.state not in safe:
        raise FlowError(
            f"workspace is not safe for flow execution: {diagnosis.state}: {diagnosis.detail}"
        )
    return diagnosis.state


def _dry_run_existing(
    control_root: Path,
    config: object,
    manifest: RunManifest,
    target: str,
) -> int:
    state = _safe_existing_state(control_root, config, manifest)
    print("FLOW_MODE=DRY_RUN")
    print(f"FLOW_RUN_ID={manifest.run_id}")
    print(f"RUN_STAGE={manifest.stage}")
    print(f"DIAGNOSIS={state}")
    print(f"FLOW_TARGET={target}")
    if _target_satisfied_by_manifest(manifest, target):
        print("NEXT_SAFE_GATE=NONE_TARGET_ALREADY_SATISFIED")
    elif state == "READY_FOR_AUDIT":
        print("NEXT_SAFE_GATE=AUDIT")
    elif state in {"READY_FOR_CHECKPOINT", "CHECKPOINT_RETRYABLE"}:
        print("NEXT_SAFE_GATE=CHECKPOINT")
    else:
        print("NEXT_SAFE_GATE=PUBLISH_VERIFY_OR_CREATE")
    print("MUTATIONS=NONE")
    print("CANONICAL_PROMOTION=NEVER_AUTOMATIC")
    print("YOUMO_PROMOTE_EXECUTE=NOT_STARTED")
    print(f"RERUN_WITH=youmo-flow --run-id {manifest.run_id} --until {target} --execute")
    return 0


def _build_new(
    control_root: Path,
    project: str,
    args: argparse.Namespace,
) -> tuple[int, str | None]:
    build_argv = [
        "--repo",
        str(control_root),
        "--project",
        project,
        "--workspace",
        args.workspace,
        "--task",
        args.task.strip(),
        "--max-changed-files",
        str(args.max_changed_files),
        "--execute",
    ]
    for path in args.allowed_paths:
        build_argv.extend(["--allow-path", path])
    rc, out, _ = _invoke("BUILD", build_main, build_argv)
    run_id: str | None = None
    try:
        run_id = _run_id_from_output(out)
    except FlowError:
        if rc == 0:
            raise
    return rc, run_id


def _resume_once(control_root: Path, project: str, run_id: str, subject: str) -> int:
    argv = [
        "--repo",
        str(control_root),
        "--project",
        project,
        "--run-id",
        run_id,
        "--subject",
        subject,
        "--execute",
    ]
    rc, _, _ = _invoke("RESUME_NEXT_GATE", resume_main, argv)
    return rc


def _publish_once(control_root: Path, project: str, run_id: str) -> int:
    rc, _, _ = _invoke(
        "PUBLISH_NONCANONICAL",
        publish_main,
        [
            "--repo",
            str(control_root),
            "--project",
            project,
            "--run-id",
            run_id,
            "--execute",
        ],
    )
    return rc


def _promotion_check_once(control_root: Path, project: str, run_id: str) -> int:
    rc, _, _ = _invoke(
        "PROMOTION_READINESS",
        promotion_check_main,
        [
            "--repo",
            str(control_root),
            "--project",
            project,
            "--run-id",
            run_id,
        ],
    )
    return rc


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _validate_arguments(args)
        control_state = inspect_repo(Path(args.repo).resolve())
        config = load_project_config(control_state.root, args.project)
    except (FlowError, GitInspectionError, ProjectConfigError) as exc:
        print(f"STOP: flow preflight failed: {exc}", file=sys.stderr)
        return 15

    preflight = run_preflight_gate(control_state.root, config, control_state)
    if not preflight.passed:
        print("STOP: controller preflight failed.", file=sys.stderr)
        for check in preflight.checks:
            if not check.passed:
                print(f"FAIL {check.name}: {check.detail}", file=sys.stderr)
        return 2

    if not args.execute:
        if args.workspace:
            return _dry_run_new(control_state.root, args.project, args)
        try:
            manifest = load_run_manifest(control_state.root, config, args.run_id)
            return _dry_run_existing(control_state.root, config, manifest, args.until)
        except (RunManifestError, FlowError) as exc:
            print(f"STOP: flow dry-run failed: {exc}", file=sys.stderr)
            return 15

    steps = 0
    run_id = args.run_id
    if args.workspace:
        rc, run_id = _build_new(control_state.root, args.project, args)
        steps += 1
        if run_id is None:
            print("STOP: build did not produce a recoverable run id", file=sys.stderr)
            return rc or 15
        if rc != 0:
            _print_boundary(run_id, args.until, steps, detail="build stopped the flow")
            return rc
        if args.until == "build":
            _print_boundary(run_id, args.until, steps, detail="requested build boundary reached")
            return 0
    assert run_id is not None

    while steps < args.max_steps:
        try:
            manifest = load_run_manifest(control_state.root, config, run_id)
            state = _safe_existing_state(control_state.root, config, manifest)
        except (RunManifestError, FlowError) as exc:
            print(f"STOP: flow state verification failed: {exc}", file=sys.stderr)
            _print_boundary(run_id, args.until, steps, detail="state verification failed")
            return 15

        if _target_satisfied_by_manifest(manifest, args.until):
            _print_boundary(run_id, args.until, steps, detail="requested boundary already satisfied")
            return 0

        if state == "READY_FOR_AUDIT":
            rc = _resume_once(control_state.root, args.project, run_id, args.subject)
            steps += 1
            if rc != 0:
                _print_boundary(run_id, args.until, steps, detail="audit stopped the flow")
                return rc
            if args.until == "audit":
                _print_boundary(run_id, args.until, steps, detail="requested audit boundary reached")
                return 0
            continue

        if state in {"READY_FOR_CHECKPOINT", "CHECKPOINT_RETRYABLE"}:
            rc = _resume_once(control_state.root, args.project, run_id, args.subject)
            steps += 1
            if rc != 0:
                _print_boundary(run_id, args.until, steps, detail="checkpoint stopped the flow")
                return rc
            if args.until == "checkpoint":
                _print_boundary(run_id, args.until, steps, detail="requested checkpoint boundary reached")
                return 0
            continue

        if state == "CHECKPOINTED_CLEAN":
            if args.until == "checkpoint":
                _print_boundary(run_id, args.until, steps, detail="checkpoint already verified")
                return 0
            rc = _publish_once(control_state.root, args.project, run_id)
            steps += 1
            if rc != 0:
                _print_boundary(run_id, args.until, steps, detail="noncanonical publication stopped the flow")
                return rc
            if args.until == "publish":
                _print_boundary(run_id, args.until, steps, detail="requested publish boundary reached")
                return 0
            if steps >= args.max_steps:
                break
            rc = _promotion_check_once(control_state.root, args.project, run_id)
            steps += 1
            _print_boundary(
                run_id,
                args.until,
                steps,
                detail=(
                    "promotion readiness checked; canonical promotion remains explicit"
                    if rc == 0
                    else "promotion readiness blocked/stopped; canonical promotion remains explicit"
                ),
            )
            return rc

    _print_boundary(
        run_id,
        args.until,
        steps,
        detail="hard step cap reached before requested boundary; rerun flow to continue safely",
    )
    print("FLOW_BOUNDED_HALT=TRUE")
    return 15


if __name__ == "__main__":
    raise SystemExit(main())
