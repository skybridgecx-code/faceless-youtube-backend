from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .codex_transport import inspect_codex_sdk
from .config import ProjectConfigError, load_project_config
from .controller import default_layout, inspect_controller
from .doctor import DoctorError, diagnose_all
from .gates import run_preflight_gate
from .git_state import GitInspectionError, inspect_repo

DEFAULT_MANIFEST = "tools/project_agent_runtime/projects/youmo.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="youmo-doctor",
        description="Read-only health and next-action diagnosis for YouMo controller/executors.",
    )
    parser.add_argument("--repo", default=".", help="read-only YouMo controller repository")
    parser.add_argument("--project", default=DEFAULT_MANIFEST)
    parser.add_argument("--workspace", help="inspect one executor instead of all registered executors")
    parser.add_argument("--controller-home", help="override controller root for diagnostics/tests")
    parser.add_argument("--json", action="store_true")
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
    controller_layout = (
        default_layout(Path(args.controller_home).expanduser())
        if args.controller_home
        else default_layout()
    )
    controller = inspect_controller(controller_layout)
    sdk = inspect_codex_sdk(config.codex.sdk_requirement)

    try:
        report = diagnose_all(
            control_state.root,
            config,
            workspace=Path(args.workspace) if args.workspace else None,
        )
    except DoctorError as exc:
        print(f"STOP: doctor state is unreadable: {exc}", file=sys.stderr)
        return 11

    healthy = preflight.passed and controller.ready and sdk.ready and report.healthy
    payload = {
        "healthy": healthy,
        "control_preflight": preflight.passed,
        "controller": {
            "installed": controller.installed,
            "ready": controller.ready,
            "head": controller.head,
            "expected_head": controller.expected_head,
            "branch": controller.branch,
            "clean": controller.clean,
            "dependencies_ready": controller.dependencies_ready,
            "launchers_ready": controller.launchers_ready,
            "push_disabled": controller.push_disabled,
            "detail": controller.detail,
        },
        "codex_sdk": {
            "ready": sdk.ready,
            "installed": sdk.installed,
            "compatible": sdk.compatible,
            "version": sdk.version,
            "requirement": sdk.requirement,
            "detail": sdk.detail,
        },
        "workspaces": [item.to_mapping() for item in report.workspaces],
        "transport_started": False,
        "mutations_performed": False,
    }

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"DOCTOR={'PASS' if healthy else 'ATTENTION'}")
        print(f"CONTROL_PREFLIGHT={'PASS' if preflight.passed else 'FAIL'}")
        print(f"CONTROLLER={'READY' if controller.ready else 'NOT_READY'}")
        print(f"CONTROLLER_PUSH_DISABLED={str(controller.push_disabled).upper()}")
        print(f"CODEX_SDK={'READY' if sdk.ready else 'NOT_READY'}")
        print(f"REGISTERED_EXECUTORS={len(report.workspaces)}")
        for item in report.workspaces:
            print(f"WORKSPACE={item.workspace}")
            print(f"STATE={item.state}")
            print(f"RUN_ID={item.run_id or '<none>'}")
            print(f"RUN_STAGE={item.run_stage or '<none>'}")
            print(f"BRANCH={item.branch or '<unknown>'}")
            print(f"HEAD={item.head or '<unknown>'}")
            print(f"LEASE={item.lease}")
            print(f"NEXT_ACTION={item.next_action}")
            for warning in item.warnings:
                print(f"WARNING={warning}")
        print("CODEX_TRANSPORT=NOT_STARTED")
        print("MUTATIONS=NONE")
    return 0 if healthy else 11


if __name__ == "__main__":
    raise SystemExit(main())
