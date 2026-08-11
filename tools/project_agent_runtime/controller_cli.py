from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Sequence

from .controller import (
    DEFAULT_CONTROLLER_REF,
    ControllerError,
    ControllerLayout,
    default_layout,
    inspect_controller,
    install_controller,
    refresh_controller,
)

_SOURCE_ROOT = Path(__file__).resolve().parents[2]


def _layout(value: str | None) -> ControllerLayout:
    if value is None:
        return default_layout()
    return default_layout(Path(value).expanduser())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="youmo-controller",
        description="Provision and inspect the isolated YouMo controller installation.",
    )
    parser.add_argument(
        "--home",
        default=None,
        help="controller root (default: ~/.youmo/controller)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="inspect the dedicated controller")
    status.add_argument("--json", action="store_true")

    for name in ("install", "refresh"):
        command = sub.add_parser(name)
        command.add_argument(
            "--sha",
            required=True,
            help="exact 40-character controller commit SHA",
        )
        command.add_argument(
            "--ref",
            default=DEFAULT_CONTROLLER_REF,
            help=f"source ref to verify (default: {DEFAULT_CONTROLLER_REF})",
        )
        command.add_argument(
            "--execute",
            action="store_true",
            help=f"perform controller {name}; omitted by default",
        )
    return parser


def _status_lines(status: object) -> list[str]:
    return [
        f"INSTALLED={str(getattr(status, 'installed')).upper()}",
        f"READY={str(getattr(status, 'ready')).upper()}",
        f"CONTROLLER_REPO={getattr(status, 'repo_path')}",
        f"CONTROLLER_VENV={getattr(status, 'venv_path')}",
        f"CONTROLLER_BIN={getattr(status, 'bin_path')}",
        f"HEAD={getattr(status, 'head') or '<none>'}",
        f"EXPECTED_HEAD={getattr(status, 'expected_head') or '<none>'}",
        f"BRANCH={getattr(status, 'branch') or '<none>'}",
        f"CLEAN={getattr(status, 'clean') if getattr(status, 'clean') is not None else '<unknown>'}",
        f"DEPENDENCIES_READY={str(getattr(status, 'dependencies_ready')).upper()}",
        f"LAUNCHERS_READY={str(getattr(status, 'launchers_ready')).upper()}",
        f"DETAIL={getattr(status, 'detail')}",
    ]


def _path_hint(layout: ControllerLayout) -> str:
    return f"export PATH={shlex.quote(str(layout.bin))}:\"$PATH\""


def _dry_run_install(layout: ControllerLayout, sha: str, ref: str) -> int:
    current = inspect_controller(layout)
    if current.installed:
        print("STOP: controller installation already exists; use refresh.", file=sys.stderr)
        return 3
    print("CONTROLLER_INSTALL=DRY_RUN")
    print(f"SOURCE_REF={ref}")
    print(f"EXPECTED_SHA={sha}")
    print(f"CONTROLLER_REPO={layout.repo}")
    print(f"CONTROLLER_VENV={layout.venv}")
    print(f"CONTROLLER_BIN={layout.bin}")
    print("ACTIVE_PROJECT_CHECKOUT_MUTATION=NONE")
    print("SHELL_PROFILE_MUTATION=NONE")
    print("GIT_PUSH=NOT_STARTED")
    print("RERUN_WITH=youmo-controller install ... --execute")
    return 0


def _dry_run_refresh(layout: ControllerLayout, sha: str, ref: str) -> int:
    current = inspect_controller(layout)
    if not current.installed:
        print("STOP: controller is not installed.", file=sys.stderr)
        return 3
    if not current.ready:
        print(
            f"STOP: controller is not currently ready: {current.detail}", file=sys.stderr
        )
        return 3
    print("CONTROLLER_REFRESH=DRY_RUN")
    print(f"CURRENT_SHA={current.head}")
    print(f"SOURCE_REF={ref}")
    print(f"EXPECTED_SHA={sha}")
    print("FAST_FORWARD_ONLY=TRUE")
    print("ACTIVE_PROJECT_CHECKOUT_MUTATION=NONE")
    print("SHELL_PROFILE_MUTATION=NONE")
    print("GIT_PUSH=NOT_STARTED")
    print("RERUN_WITH=youmo-controller refresh ... --execute")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    layout = _layout(args.home)

    if args.command == "status":
        status = inspect_controller(layout)
        if args.json:
            print(status.to_json(), end="")
        else:
            for line in _status_lines(status):
                print(line)
            print(f"PATH_HINT={_path_hint(layout)}")
        return 0 if status.ready else 3

    sha = args.sha.strip().lower()
    ref = args.ref.strip()
    if not args.execute:
        if args.command == "install":
            return _dry_run_install(layout, sha, ref)
        return _dry_run_refresh(layout, sha, ref)

    try:
        if args.command == "install":
            status = install_controller(
                expected_sha=sha,
                source_ref=ref,
                layout=layout,
                source_root=_SOURCE_ROOT,
            )
        else:
            status = refresh_controller(
                expected_sha=sha,
                source_ref=ref,
                layout=layout,
            )
    except ControllerError as exc:
        print(f"STOP: controller {args.command} failed: {exc}", file=sys.stderr)
        return 3

    for line in _status_lines(status):
        print(line)
    print("ACTIVE_PROJECT_CHECKOUT_MUTATION=NONE")
    print("SHELL_PROFILE_MUTATION=NONE")
    print("GIT_PUSH=NOT_STARTED")
    print(f"PATH_HINT={_path_hint(layout)}")
    return 0 if status.ready else 3


if __name__ == "__main__":
    raise SystemExit(main())
