from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path
from typing import Sequence

from .controller import ControllerError, ControllerLayout, default_layout
from .controller_release import (
    CONTROLLER_RELEASE_REF,
    inspect_controller_release,
    install_controller_release,
    refresh_controller_release,
)

_SOURCE_ROOT = Path(__file__).resolve().parents[2]


def _layout(value: str | None) -> ControllerLayout:
    return default_layout(Path(value).expanduser()) if value else default_layout()


def _validated_sha(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 40 or any(ch not in "0123456789abcdef" for ch in normalized):
        raise ControllerError("--sha must be a full 40-character hexadecimal commit SHA")
    return normalized


def _path_hint(layout: ControllerLayout) -> str:
    return f"export PATH={shlex.quote(str(layout.bin))}:\"$PATH\""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="youmo-controller",
        description="Provision and inspect the isolated YouMo controller release.",
    )
    parser.add_argument("--home", help="controller root (default: ~/.youmo/controller)")
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status")
    status.add_argument("--json", action="store_true")
    for name in ("install", "refresh"):
        command = sub.add_parser(name)
        command.add_argument("--sha", required=True)
        command.add_argument("--ref", default=CONTROLLER_RELEASE_REF)
        command.add_argument("--execute", action="store_true")
    return parser


def _status_lines(status: object) -> list[str]:
    clean = getattr(status, "clean")
    return [
        f"INSTALLED={str(getattr(status, 'installed')).upper()}",
        f"READY={str(getattr(status, 'ready')).upper()}",
        f"CONTROLLER_REPO={getattr(status, 'repo_path')}",
        f"CONTROLLER_VENV={getattr(status, 'venv_path')}",
        f"CONTROLLER_BIN={getattr(status, 'bin_path')}",
        f"HEAD={getattr(status, 'head') or '<none>'}",
        f"EXPECTED_HEAD={getattr(status, 'expected_head') or '<none>'}",
        f"BRANCH={getattr(status, 'branch') or '<none>'}",
        f"CLEAN={clean if clean is not None else '<unknown>'}",
        f"DEPENDENCIES_READY={str(getattr(status, 'dependencies_ready')).upper()}",
        f"LAUNCHERS_READY={str(getattr(status, 'launchers_ready')).upper()}",
        f"PUSH_DISABLED={str(getattr(status, 'push_disabled')).upper()}",
        f"DETAIL={getattr(status, 'detail')}",
    ]


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    layout = _layout(args.home)
    if args.command == "status":
        status = inspect_controller_release(layout)
        if args.json:
            print(status.to_json(), end="")
        else:
            for line in _status_lines(status):
                print(line)
            print(f"PATH_HINT={_path_hint(layout)}")
        return 0 if status.ready else 3

    try:
        sha = _validated_sha(args.sha)
    except ControllerError as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 3
    ref = args.ref.strip()
    if not ref:
        print("STOP: --ref must be non-empty", file=sys.stderr)
        return 3

    if not args.execute:
        current = inspect_controller_release(layout)
        if args.command == "install" and current.installed:
            print("STOP: controller installation already exists; use refresh.", file=sys.stderr)
            return 3
        if args.command == "refresh" and not current.ready:
            print(f"STOP: controller is not ready: {current.detail}", file=sys.stderr)
            return 3
        print(f"CONTROLLER_{args.command.upper()}=DRY_RUN")
        if args.command == "refresh":
            print(f"CURRENT_SHA={current.head}")
            print("FAST_FORWARD_ONLY=TRUE")
        print(f"SOURCE_REF={ref}")
        print(f"EXPECTED_SHA={sha}")
        print(f"CONTROLLER_REPO={layout.repo}")
        print(f"CONTROLLER_VENV={layout.venv}")
        print(f"CONTROLLER_BIN={layout.bin}")
        print("MANAGED_LAUNCHERS=youmo,youmo-build,youmo-audit,youmo-checkpoint,youmo-controller,youmo-doctor,youmo-resume")
        print("CONTROLLER_PUSH_POLICY=DISABLED")
        print("ACTIVE_PROJECT_CHECKOUT_MUTATION=NONE")
        print("SHELL_PROFILE_MUTATION=NONE")
        print("GIT_PUSH=NOT_STARTED")
        print(f"RERUN_WITH=youmo-controller {args.command} ... --execute")
        return 0

    try:
        if args.command == "install":
            status = install_controller_release(
                expected_sha=sha,
                source_ref=ref,
                layout=layout,
                source_root=_SOURCE_ROOT,
            )
        else:
            status = refresh_controller_release(
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
