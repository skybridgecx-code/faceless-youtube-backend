from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .audit_cli import main as audit_main
from .checkpoint_cli import main as checkpoint_main
from .config import ProjectConfigError, load_project_config
from .doctor import diagnose_workspace
from .gates import run_preflight_gate
from .git_state import GitInspectionError, inspect_repo
from .run_manifest import RunManifestError, load_run_manifest

DEFAULT_MANIFEST = "tools/project_agent_runtime/projects/youmo.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="youmo-resume",
        description="Resume exactly the next authorized gate for an immutable YouMo run.",
    )
    parser.add_argument("--repo", default=".", help="read-only YouMo controller repository")
    parser.add_argument("--project", default=DEFAULT_MANIFEST)
    parser.add_argument("--run-id", required=True, help="immutable YouMo run identifier")
    parser.add_argument("--subject", default="YouMo audited checkpoint")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="run the next authorized gate; omitted by default",
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
        print("STOP: control-repository preflight failed.", file=sys.stderr)
        return 2

    try:
        manifest = load_run_manifest(control_state.root, config, args.run_id)
    except RunManifestError as exc:
        print(f"STOP: run manifest load failed: {exc}", file=sys.stderr)
        return 11

    diagnosis = diagnose_workspace(
        control_state.root, config, Path(manifest.workspace)
    )
    if diagnosis.run_id != manifest.run_id and diagnosis.state != "LEASE_ACTIVE":
        print(
            "STOP: requested run is not the latest bound run for its executor; "
            "resume will not bypass a newer run.",
            file=sys.stderr,
        )
        return 11

    if diagnosis.state == "CHECKPOINTED_CLEAN":
        print(f"RUN_ID={manifest.run_id}")
        print("RUN_STAGE=CHECKPOINTED")
        print(f"COMMIT_SHA={manifest.checkpoint_commit}")
        print("NEXT_GATE=NONE")
        print("GIT_PUSH=NOT_STARTED")
        return 0

    if diagnosis.state == "READY_FOR_AUDIT":
        action = "AUDIT"
        command = f"youmo-audit --run-id {manifest.run_id}"
    elif diagnosis.state in {"READY_FOR_CHECKPOINT", "CHECKPOINT_RETRYABLE"}:
        action = "CHECKPOINT"
        command = f"youmo-checkpoint --run-id {manifest.run_id}"
    else:
        print(f"RUN_ID={manifest.run_id}")
        print(f"RESUME_FROM_STAGE={manifest.stage}")
        print(f"DIAGNOSIS={diagnosis.state}")
        print(f"DETAIL={diagnosis.detail}")
        print(f"NEXT_ACTION={diagnosis.next_action}")
        print(
            "STOP: this state is not safe for automatic resume; no mutation was performed.",
            file=sys.stderr,
        )
        return 11

    print(f"RUN_ID={manifest.run_id}")
    print(f"RESUME_FROM_STAGE={manifest.stage}")
    print(f"DIAGNOSIS={diagnosis.state}")
    print(f"NEXT_GATE={action}")
    if not args.execute:
        print("EXECUTION=DRY_RUN")
        print(f"RERUN_WITH={command} --execute")
        print("MUTATIONS=NONE")
        return 0

    if action == "AUDIT":
        return audit_main(
            [
                "--repo",
                str(control_state.root),
                "--project",
                args.project,
                "--run-id",
                manifest.run_id,
                "--execute",
            ]
        )
    return checkpoint_main(
        [
            "--repo",
            str(control_state.root),
            "--project",
            args.project,
            "--run-id",
            manifest.run_id,
            "--subject",
            args.subject,
            "--execute",
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
