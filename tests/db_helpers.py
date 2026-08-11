from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import close_all_sessions

from app import models  # noqa: F401
from app.db import Base, engine
from app.db.schema import DatabaseSchemaError, expected_head_revision, validate_database_schema
from scripts.migrate_db import upgrade_database


REPO_ROOT = Path(__file__).resolve().parents[1]
PROTECTED_DATABASE = (REPO_ROOT / "content_factory.db").resolve()


def _template_path(target: Path, head_revision: str) -> Path:
    template_key = hashlib.sha256(f"{target}:{head_revision}".encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"faceless-i2-test-schema-{head_revision}-{template_key}.db"


def _template_is_valid(template: Path) -> bool:
    if not template.is_file():
        return False
    template_engine = create_engine(f"sqlite:///{template}", future=True)
    try:
        validate_database_schema(template_engine, Base.metadata.tables.values())
    except (DatabaseSchemaError, OSError, SQLAlchemyError):
        return False
    finally:
        template_engine.dispose()
    return True


def _ensure_migrated_template(target: Path) -> Path:
    head_revision = expected_head_revision()
    template = _template_path(target, head_revision)
    if _template_is_valid(template):
        return template

    descriptor, candidate_name = tempfile.mkstemp(
        prefix=f"faceless-i2-test-schema-{head_revision}-",
        suffix=".db",
    )
    os.close(descriptor)
    candidate = Path(candidate_name)
    try:
        upgrade_database(f"sqlite:///{candidate}")
        if not _template_is_valid(candidate):
            raise RuntimeError(f"Alembic produced an invalid test schema template at {candidate}")
        os.replace(candidate, template)
    finally:
        if candidate.exists():
            candidate.unlink()
    return template


def reset_migrated_test_database() -> None:
    """Recreate the configured test SQLite file through Alembic, never metadata DDL."""

    url = str(engine.url)
    parsed = make_url(url)
    if not parsed.drivername.startswith("sqlite") or not parsed.database or parsed.database == ":memory:":
        raise RuntimeError("Tests require an explicit isolated SQLite database file.")

    path = Path(parsed.database).resolve()
    if path == PROTECTED_DATABASE or not path.name.startswith("test_"):
        raise RuntimeError(f"Refusing to reset non-test database: {path}")

    close_all_sessions()
    engine.dispose()
    template = _ensure_migrated_template(path)
    for candidate in (path, path.with_name(f"{path.name}-wal"), path.with_name(f"{path.name}-shm")):
        if candidate.exists():
            candidate.unlink()

    shutil.copy2(template, path)
    engine.dispose()
    validate_database_schema(engine, Base.metadata.tables.values())
