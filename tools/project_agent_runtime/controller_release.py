from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Callable

from .controller import (
    MANAGED_LAUNCHER_MARKER,
    ControllerError,
    ControllerLayout,
    ControllerStatus,
    CommandRunner,
    _launcher_content,
    default_layout,
    inspect_controller as inspect_base_controller,
    install_controller as install_base_controller,
    refresh_controller as refresh_base_controller,
)


CONTROLLER_RELEASE_REF = "tooling/youmo-cli-i9"
EXTRA_LAUNCHERS: tuple[str, ...] = ("youmo-doctor", "youmo-resume")


def _extra_launcher_target(layout: ControllerLayout, name: str) -> Path:
    return layout.bin / name


def _preflight_extra_launchers(layout: ControllerLayout) -> None:
    for name in EXTRA_LAUNCHERS:
        target = _extra_launcher_target(layout, name)
        if target.exists() and not target.is_file():
            raise ControllerError(
                f"release launcher path exists and is not a regular file: {target}"
            )
        if target.is_file():
            try:
                existing = target.read_text(encoding="utf-8")
            except OSError as exc:
                raise ControllerError(f"release launcher is unreadable: {target}") from exc
            if MANAGED_LAUNCHER_MARKER not in existing:
                raise ControllerError(f"refusing to overwrite unmanaged launcher: {target}")


def _install_extra_launchers(layout: ControllerLayout) -> None:
    _preflight_extra_launchers(layout)
    layout.bin.mkdir(parents=True, exist_ok=True)
    for name in EXTRA_LAUNCHERS:
        source = layout.repo / "scripts" / name
        if not source.is_file():
            raise ControllerError(f"controller release launcher source is missing: {source}")
        target = _extra_launcher_target(layout, name)
        content = _launcher_content(layout, name)
        if target.is_file() and target.read_text(encoding="utf-8") == content:
            os.chmod(target, 0o755)
            continue
        temp = target.with_name(target.name + f".tmp-{os.getpid()}")
        try:
            temp.write_text(content, encoding="utf-8")
            os.chmod(temp, 0o755)
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)


def _extra_launchers_ready(layout: ControllerLayout) -> bool:
    for name in EXTRA_LAUNCHERS:
        source = layout.repo / "scripts" / name
        target = _extra_launcher_target(layout, name)
        if not source.is_file() or not target.is_file():
            return False
        try:
            actual = target.read_text(encoding="utf-8")
        except OSError:
            return False
        if actual != _launcher_content(layout, name):
            return False
    return True


def inspect_controller_release(
    layout: ControllerLayout | None = None,
) -> ControllerStatus:
    resolved = layout or default_layout()
    base = inspect_base_controller(resolved)
    extras_ready = base.installed and _extra_launchers_ready(resolved)
    ready = base.ready and extras_ready
    if ready:
        detail = "controller release ready"
    elif base.ready and not extras_ready:
        detail = "controller core ready but doctor/resume launchers are missing or stale"
    else:
        detail = base.detail
    return replace(
        base,
        ready=ready,
        launchers_ready=base.launchers_ready and extras_ready,
        detail=detail,
    )


def install_controller_release(
    *,
    expected_sha: str,
    source_ref: str = CONTROLLER_RELEASE_REF,
    layout: ControllerLayout | None = None,
    source_root: Path | None = None,
    repository_url: str = "https://github.com/skybridgecx-code/faceless-youtube-backend.git",
    clone_source_url: str | None = None,
    python_executable: str | None = None,
    runner: CommandRunner | None = None,
) -> ControllerStatus:
    resolved = layout or default_layout()
    _preflight_extra_launchers(resolved)
    kwargs: dict[str, object] = {
        "expected_sha": expected_sha,
        "source_ref": source_ref,
        "layout": resolved,
        "source_root": source_root,
        "repository_url": repository_url,
        "clone_source_url": clone_source_url,
    }
    if python_executable is not None:
        kwargs["python_executable"] = python_executable
    if runner is not None:
        kwargs["runner"] = runner
    install_base_controller(**kwargs)
    _install_extra_launchers(resolved)
    status = inspect_controller_release(resolved)
    if not status.ready:
        raise ControllerError(f"controller release failed post-install readiness: {status.detail}")
    return status


def refresh_controller_release(
    *,
    expected_sha: str,
    source_ref: str = CONTROLLER_RELEASE_REF,
    layout: ControllerLayout | None = None,
    runner: CommandRunner | None = None,
) -> ControllerStatus:
    resolved = layout or default_layout()
    _preflight_extra_launchers(resolved)
    kwargs: dict[str, object] = {
        "expected_sha": expected_sha,
        "source_ref": source_ref,
        "layout": resolved,
    }
    if runner is not None:
        kwargs["runner"] = runner
    refresh_base_controller(**kwargs)
    _install_extra_launchers(resolved)
    status = inspect_controller_release(resolved)
    if not status.ready:
        raise ControllerError(f"controller release failed post-refresh readiness: {status.detail}")
    return status
