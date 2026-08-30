"""Session-owned cleanup for the legacy test content-factory SQLite artifacts."""

from __future__ import annotations

from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_ARTIFACTS = tuple(
    _ROOT / name
    for name in (
        "test_content_factory.db",
        "test_content_factory.db-journal",
        "test_content_factory.db-wal",
        "test_content_factory.db-shm",
    )
)
_PREEXISTING: dict[Path, bool] = {}


def pytest_sessionstart(session: object) -> None:
    del session
    _PREEXISTING.clear()
    _PREEXISTING.update({path: path.exists() for path in _ARTIFACTS})


def _remove_session_owned_artifacts() -> None:
    failures: list[str] = []
    for path in _ARTIFACTS:
        if _PREEXISTING.get(path, True) or not path.exists():
            continue
        try:
            path.unlink()
        except OSError as exc:
            failures.append(f"{path.name}: {exc}")
    if failures:
        raise RuntimeError("pytest could not remove session-owned database artifacts: " + "; ".join(failures))


def pytest_sessionfinish(session: object, exitstatus: int) -> None:
    del session, exitstatus
    _remove_session_owned_artifacts()


def pytest_unconfigure(config: object) -> None:
    del config
    _remove_session_owned_artifacts()
