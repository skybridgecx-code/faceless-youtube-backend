from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import ProjectConfig
from .state import state_directory


class OperationLeaseError(RuntimeError):
    """Raised when an executor already has an active YouMo operation."""


@dataclass(frozen=True)
class OperationLease:
    schema_version: int
    project_id: str
    workspace: str
    operation: str
    pid: int
    hostname: str
    started_at: str
    token: str

    @classmethod
    def from_mapping(cls, payload: dict[str, object]) -> "OperationLease":
        if payload.get("schema_version") != 1:
            raise OperationLeaseError("unsupported operation lease schema")
        values: dict[str, str] = {}
        for key in ("project_id", "workspace", "operation", "hostname", "started_at", "token"):
            value = payload.get(key)
            if not isinstance(value, str) or not value:
                raise OperationLeaseError(f"operation lease {key} is invalid")
            values[key] = value
        pid = payload.get("pid")
        if not isinstance(pid, int) or pid < 1:
            raise OperationLeaseError("operation lease pid is invalid")
        return cls(schema_version=1, pid=pid, **values)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lease_path(control_root: Path, config: ProjectConfig, workspace: Path) -> Path:
    identity = hashlib.sha256(str(workspace.resolve()).encode("utf-8")).hexdigest()[:24]
    return state_directory(control_root, config.state_dir) / "operation-leases" / f"{identity}.json"


def _read_lease(path: Path) -> OperationLease:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OperationLeaseError("operation lease disappeared during inspection") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise OperationLeaseError(f"operation lease is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise OperationLeaseError("operation lease root must be an object")
    return OperationLease.from_mapping(payload)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _write_new_lease(path: Path, lease: OperationLease) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(asdict(lease), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _acquire_once(
    path: Path,
    *,
    config: ProjectConfig,
    workspace: Path,
    operation: str,
) -> OperationLease:
    lease = OperationLease(
        schema_version=1,
        project_id=config.project_id,
        workspace=str(workspace.resolve()),
        operation=operation,
        pid=os.getpid(),
        hostname=socket.gethostname(),
        started_at=_utc_now(),
        token=secrets.token_hex(16),
    )
    _write_new_lease(path, lease)
    return lease


def acquire_operation_lease(
    control_root: Path,
    config: ProjectConfig,
    workspace: Path,
    operation: str,
) -> tuple[Path, OperationLease]:
    operation = operation.strip()
    if not operation:
        raise OperationLeaseError("operation name must be non-empty")
    resolved_workspace = workspace.resolve()
    path = _lease_path(control_root.resolve(), config, resolved_workspace)
    try:
        return path, _acquire_once(
            path, config=config, workspace=resolved_workspace, operation=operation
        )
    except FileExistsError:
        existing = _read_lease(path)
        same_host = existing.hostname == socket.gethostname()
        if same_host and not _pid_alive(existing.pid):
            try:
                path.unlink()
            except OSError as exc:
                raise OperationLeaseError(
                    f"stale operation lease could not be removed: {path}"
                ) from exc
            try:
                return path, _acquire_once(
                    path, config=config, workspace=resolved_workspace, operation=operation
                )
            except FileExistsError as exc:
                raise OperationLeaseError(
                    "another YouMo operation acquired the executor during stale-lease recovery"
                ) from exc
        raise OperationLeaseError(
            "executor is already leased by "
            f"operation={existing.operation!r} pid={existing.pid} host={existing.hostname!r} "
            f"started_at={existing.started_at}"
        )


def release_operation_lease(path: Path, lease: OperationLease) -> None:
    if not path.exists():
        raise OperationLeaseError("operation lease disappeared before release")
    current = _read_lease(path)
    if current.token != lease.token:
        raise OperationLeaseError("operation lease ownership changed before release")
    try:
        path.unlink()
    except OSError as exc:
        raise OperationLeaseError(f"failed to release operation lease: {path}") from exc


@contextmanager
def operation_lease(
    control_root: Path,
    config: ProjectConfig,
    workspace: Path,
    operation: str,
) -> Iterator[OperationLease]:
    path, lease = acquire_operation_lease(control_root, config, workspace, operation)
    try:
        yield lease
    finally:
        release_operation_lease(path, lease)
