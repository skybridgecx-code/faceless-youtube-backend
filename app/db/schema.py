from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect, text


REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_CONFIG_PATH = REPO_ROOT / "alembic.ini"


class DatabaseSchemaError(RuntimeError):
    """Raised when the application database has not been explicitly migrated."""


def expected_head_revision() -> str:
    config = Config(str(ALEMBIC_CONFIG_PATH))
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    if head is None:
        raise DatabaseSchemaError("Alembic has no configured head revision.")
    return head


def validate_database_schema(engine: Engine, required_tables: Iterable[object]) -> None:
    """Verify schema readiness without making any database mutation."""

    inspector = inspect(engine)
    if not inspector.has_table("alembic_version"):
        raise DatabaseSchemaError(
            "Database is not Alembic-versioned. Run scripts/migrate_db.py against an explicit database copy."
        )

    with engine.connect() as connection:
        revisions = [row[0] for row in connection.execute(text("SELECT version_num FROM alembic_version"))]

    expected_head = expected_head_revision()
    if revisions != [expected_head]:
        raise DatabaseSchemaError(
            f"Database revision {revisions or 'missing'} is not the expected head {expected_head}. "
            "Run alembic upgrade head through scripts/migrate_db.py."
        )

    expected_columns = {
        table.name: {column.name for column in table.columns}
        for table in required_tables
    }
    missing_tables = sorted(set(expected_columns) - set(inspector.get_table_names()))
    if missing_tables:
        raise DatabaseSchemaError(
            "Database is missing required migrated tables: " + ", ".join(missing_tables)
        )

    missing_columns = {
        table_name: sorted(columns - {column["name"] for column in inspector.get_columns(table_name)})
        for table_name, columns in expected_columns.items()
    }
    missing_columns = {table: columns for table, columns in missing_columns.items() if columns}
    if missing_columns:
        formatted = "; ".join(f"{table}: {', '.join(columns)}" for table, columns in missing_columns.items())
        raise DatabaseSchemaError("Database migration is incomplete; missing columns: " + formatted)
