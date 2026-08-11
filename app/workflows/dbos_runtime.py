from __future__ import annotations

import os
import threading
from pathlib import Path

from dbos import DBOS, DBOSConfig
from sqlalchemy.engine import URL, make_url

from app.config import Settings


DBOS_APPLICATION_NAME = "faceless-youtube-backend"
DBOS_APPLICATION_VERSION = "i3-durable-workflow-v1"
DBOS_SHUTDOWN_TIMEOUT_SECONDS = 5


class DBOSRuntimeError(RuntimeError):
    pass


class DBOSDatabaseSeparationError(DBOSRuntimeError):
    pass


_runtime_lock = threading.RLock()
_runtime_active = False
_runtime_system_identity: tuple[str, str] | None = None


def _sqlite_path(url: URL) -> Path | None:
    if not url.drivername.startswith("sqlite"):
        return None
    database = url.database
    if not database or database == ":memory:":
        return None
    return Path(database).expanduser().resolve()


def validate_dbos_database_separation(
    application_database_url: str,
    system_database_url: str,
) -> None:
    """Fail closed if DBOS could place its system schema in the application DB."""

    app_url = make_url(application_database_url)
    system_url = make_url(system_database_url)
    app_path = _sqlite_path(app_url)
    system_path = _sqlite_path(system_url)

    if system_url.drivername.startswith("sqlite") and system_path is None:
        raise DBOSDatabaseSeparationError(
            "DBOS requires a dedicated file-backed SQLite system database."
        )

    same_identity = app_url == system_url
    same_path = app_path is not None and system_path is not None and app_path == system_path
    same_file = False
    if app_path is not None and system_path is not None and app_path.exists() and system_path.exists():
        try:
            same_file = os.path.samefile(app_path, system_path)
        except OSError:
            same_file = False

    if same_identity or same_path or same_file:
        raise DBOSDatabaseSeparationError(
            "DBOS system database must be separate from the application database."
        )


def _database_identity(database_url: str) -> tuple[str, str]:
    url = make_url(database_url)
    path = _sqlite_path(url)
    if path is not None:
        return ("sqlite", str(path))
    return (url.drivername, url.render_as_string(hide_password=True))


def launch_dbos_runtime(settings: Settings) -> None:
    global _runtime_active, _runtime_system_identity
    with _runtime_lock:
        if _runtime_active:
            if _runtime_system_identity == _database_identity(
                settings.dbos_system_database_url
            ):
                return
            raise DBOSRuntimeError(
                "DBOS runtime is already active with a different system database."
            )

        validate_dbos_database_separation(
            settings.database_url,
            settings.dbos_system_database_url,
        )
        config: DBOSConfig = {
            "name": DBOS_APPLICATION_NAME,
            "system_database_url": settings.dbos_system_database_url,
            "application_version": DBOS_APPLICATION_VERSION,
            "executor_id": "local",
            "run_admin_server": False,
            "enable_otlp": False,
            "console_log_level": "WARNING",
            "use_listen_notify": False,
            "max_executor_threads": 4,
            "notification_listener_polling_interval_sec": 0.01,
            "scheduler_polling_interval_sec": 30.0,
        }
        try:
            DBOS(config=config)
            DBOS.launch()
        except Exception:
            DBOS.destroy()
            raise
        _runtime_active = True
        _runtime_system_identity = _database_identity(
            settings.dbos_system_database_url
        )


def shutdown_dbos_runtime() -> None:
    global _runtime_active, _runtime_system_identity
    with _runtime_lock:
        if not _runtime_active:
            return
        DBOS.destroy(
            workflow_completion_timeout_sec=DBOS_SHUTDOWN_TIMEOUT_SECONDS,
        )
        _runtime_active = False
        _runtime_system_identity = None


def require_dbos_runtime() -> None:
    if not _runtime_active:
        raise DBOSRuntimeError("DBOS runtime is not launched.")


def dbos_runtime_is_active() -> bool:
    return _runtime_active
