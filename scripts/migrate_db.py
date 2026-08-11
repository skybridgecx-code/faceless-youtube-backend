#!/usr/bin/env python3
"""Explicit, fail-closed Alembic migration and legacy-adoption utility."""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

BASELINE_REVISION = "22348998243b"
ORIGINAL_DATABASE = (REPO_ROOT / "content_factory.db").resolve()
CANONICAL_TABLES = {
    "campaign_budget_overrides",
    "campaigns",
    "sources",
    "claims",
    "claim_sources",
    "artifacts",
    "scenes",
    "generation_jobs",
    "gate_decisions",
    "approvals",
    "metric_snapshots",
}
LEGACY_TABLES = {
    "audit_events",
    "autopilot_runs",
    "channel_studio_agents",
    "channels",
    "content_agents",
    "content_assets",
    "executive_producer_recommendations",
    "production_briefs",
    "publish_records",
    "publishing_payloads",
    "research_patterns",
    "research_runs",
    "research_source_channels",
    "research_source_videos",
    "research_strategies",
    "reviews",
    "video_opportunities",
    "video_performance_metrics",
    "videos",
    "visual_asset_plans",
    "visual_asset_prompts",
    "visual_generated_assets",
    "visual_generation_jobs",
    "visual_scenes",
}
TOLERATED_ADDITIVE_FK_GAPS = {
    ("executive_producer_recommendations", ("matched_agent_id",), "content_agents", ("id",)),
    ("video_opportunities", ("assigned_agent_id",), "content_agents", ("id",)),
    ("videos", ("assigned_agent_id",), "content_agents", ("id",)),
    ("videos", ("channel_studio_agent_id",), "channel_studio_agents", ("id",)),
}
TOLERATED_ADDITIVE_COLUMN_SHAPE_GAPS = {
    ("video_opportunities", "review_status"),
    ("videos", "final_approval_status"),
    ("videos", "preview_reviewed"),
    ("visual_scenes", "asset_status"),
}


class MigrationSafetyError(RuntimeError):
    """Raised before an unsafe migration or Alembic stamp can occur."""


@dataclass(frozen=True)
class MigrationResult:
    state: str
    revision: str
    backup_path: Path | None


@dataclass(frozen=True)
class ColumnManifest:
    affinity: str
    not_null: bool
    default_sql: str | None
    primary_key_position: int


@dataclass(frozen=True)
class TableManifest:
    columns: dict[str, ColumnManifest]
    primary_key: tuple[str, ...]
    foreign_keys: frozenset[tuple[tuple[str, ...], str, tuple[str, ...]]]
    unique_indexes: frozenset[tuple[str, ...]]


def _config(database_url: str) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["database_url"] = database_url
    return config


def _head_revision(config: Config) -> str:
    head = ScriptDirectory.from_config(config).get_current_head()
    if head is None:
        raise MigrationSafetyError("Alembic has no current head revision.")
    return head


def sqlite_path(database_url: str) -> Path | None:
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite") or not url.database or url.database == ":memory:":
        return None
    return Path(url.database).expanduser().resolve()


def _assert_not_original_database(database_url: str) -> Path | None:
    target = sqlite_path(database_url)
    same_path = target is not None and target == ORIGINAL_DATABASE
    same_file = False
    if target is not None and target.exists() and ORIGINAL_DATABASE.exists():
        try:
            same_file = os.path.samefile(target, ORIGINAL_DATABASE)
        except OSError:
            same_file = False
    if same_path or same_file:
        raise MigrationSafetyError(
            "Refusing to migrate the protected original content_factory.db. Create and migrate an explicit copy."
        )
    return target


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _sqlite_affinity(declared_type: str) -> str:
    normalized = declared_type.upper()
    if "INT" in normalized:
        return "INTEGER"
    if any(token in normalized for token in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if not normalized or "BLOB" in normalized:
        return "BLOB"
    if any(token in normalized for token in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def _table_manifest(connection: sqlite3.Connection, table_name: str) -> TableManifest:
    quoted_table = _quote_identifier(table_name)
    column_rows = list(connection.execute(f"PRAGMA table_info({quoted_table})"))
    columns = {
        str(row[1]): ColumnManifest(
            affinity=_sqlite_affinity(str(row[2] or "")),
            not_null=bool(row[3]),
            default_sql=None if row[4] is None else str(row[4]),
            primary_key_position=int(row[5]),
        )
        for row in column_rows
    }
    primary_key = tuple(
        name
        for _, name in sorted(
            (manifest.primary_key_position, name)
            for name, manifest in columns.items()
            if manifest.primary_key_position
        )
    )

    grouped_foreign_keys: dict[int, list[tuple[object, ...]]] = {}
    for row in connection.execute(f"PRAGMA foreign_key_list({quoted_table})"):
        grouped_foreign_keys.setdefault(int(row[0]), []).append(row)
    foreign_keys = frozenset(
        (
            tuple(str(row[3]) for row in sorted(rows, key=lambda item: int(item[1]))),
            str(rows[0][2]),
            tuple(str(row[4]) for row in sorted(rows, key=lambda item: int(item[1]))),
        )
        for rows in grouped_foreign_keys.values()
    )

    unique_indexes: set[tuple[str, ...]] = set()
    for index_row in connection.execute(f"PRAGMA index_list({quoted_table})"):
        if not bool(index_row[2]):
            continue
        index_name = str(index_row[1])
        index_columns = tuple(
            str(row[2])
            for row in sorted(
                connection.execute(f"PRAGMA index_info({_quote_identifier(index_name)})"),
                key=lambda item: int(item[0]),
            )
        )
        if index_columns:
            unique_indexes.add(index_columns)

    return TableManifest(
        columns=columns,
        primary_key=primary_key,
        foreign_keys=foreign_keys,
        unique_indexes=frozenset(unique_indexes),
    )


def _schema_manifest(path: Path) -> dict[str, TableManifest]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = sorted(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
            )
        )
        return {table: _table_manifest(connection, table) for table in tables}
    finally:
        connection.close()


@lru_cache(maxsize=1)
def _baseline_manifest() -> dict[str, TableManifest]:
    with tempfile.TemporaryDirectory(prefix="faceless-i2-baseline-manifest-") as directory:
        database = Path(directory) / "baseline.db"
        command.upgrade(_config(f"sqlite:///{database}"), BASELINE_REVISION)
        manifest = _schema_manifest(database)
    if set(manifest) != LEGACY_TABLES:
        missing = sorted(LEGACY_TABLES - set(manifest))
        unexpected = sorted(set(manifest) - LEGACY_TABLES)
        raise MigrationSafetyError(
            f"Historical baseline manifest mismatch; missing={missing}, unexpected={unexpected}."
        )
    return manifest


def validate_unversioned_legacy_sqlite(path: Path) -> None:
    candidate = _schema_manifest(path)
    expected = _baseline_manifest()
    missing_tables = sorted(set(expected) - set(candidate))
    if missing_tables:
        raise MigrationSafetyError("Legacy adoption rejected; missing tables: " + ", ".join(missing_tables))
    unexpected_canonical = sorted(CANONICAL_TABLES & set(candidate))
    if unexpected_canonical:
        raise MigrationSafetyError(
            "Legacy adoption rejected; unversioned database already has canonical tables: "
            + ", ".join(unexpected_canonical)
        )

    missing_columns = {
        table: sorted(set(expected_table.columns) - set(candidate[table].columns))
        for table, expected_table in expected.items()
        if set(expected_table.columns) - set(candidate[table].columns)
    }
    if missing_columns:
        details = "; ".join(f"{table}: {', '.join(names)}" for table, names in missing_columns.items())
        raise MigrationSafetyError("Legacy adoption rejected; missing required columns: " + details)

    for table, expected_table in expected.items():
        candidate_table = candidate[table]
        affinity_mismatches = sorted(
            column
            for column, manifest in expected_table.columns.items()
            if candidate_table.columns[column].affinity != manifest.affinity
        )
        if affinity_mismatches:
            raise MigrationSafetyError(
                f"Legacy adoption rejected; incompatible column type affinity in {table}: "
                + ", ".join(affinity_mismatches)
            )
        shape_mismatches = sorted(
            column
            for column, manifest in expected_table.columns.items()
            if (table, column) not in TOLERATED_ADDITIVE_COLUMN_SHAPE_GAPS
            and (
                candidate_table.columns[column].not_null != manifest.not_null
                or candidate_table.columns[column].default_sql != manifest.default_sql
            )
        )
        if shape_mismatches:
            raise MigrationSafetyError(
                f"Legacy adoption rejected; incompatible null/default column shape in {table}: "
                + ", ".join(shape_mismatches)
            )
        if candidate_table.primary_key != expected_table.primary_key:
            raise MigrationSafetyError(
                f"Legacy adoption rejected; primary key mismatch in {table}: "
                f"expected {expected_table.primary_key}, found {candidate_table.primary_key}."
            )
        missing_unique = expected_table.unique_indexes - candidate_table.unique_indexes
        if missing_unique:
            raise MigrationSafetyError(
                f"Legacy adoption rejected; missing unique constraint/index semantics in {table}: "
                f"{sorted(missing_unique)}."
            )
        required_foreign_keys = {
            foreign_key
            for foreign_key in expected_table.foreign_keys
            if (table, foreign_key[0], foreign_key[1], foreign_key[2]) not in TOLERATED_ADDITIVE_FK_GAPS
        }
        missing_foreign_keys = required_foreign_keys - candidate_table.foreign_keys
        if missing_foreign_keys:
            raise MigrationSafetyError(
                f"Legacy adoption rejected; missing baseline foreign key relationships in {table}: "
                f"{sorted(missing_foreign_keys)}."
            )


def _versions(path: Path) -> list[str] | None:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        has_version_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
        ).fetchone()
        if not has_version_table:
            return None
        return [str(row[0]) for row in connection.execute("SELECT version_num FROM alembic_version")]
    finally:
        connection.close()


def _known_revisions(config: Config) -> set[str]:
    return {revision.revision for revision in ScriptDirectory.from_config(config).walk_revisions()}


def _logical_database_digest(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    schema_rows = list(
        connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name, tbl_name"
        )
    )
    digest.update(repr(schema_rows).encode())
    table_names = [
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    ]
    for table_name in table_names:
        quoted_table = _quote_identifier(table_name)
        column_names = [str(row[1]) for row in connection.execute(f"PRAGMA table_info({quoted_table})")]
        digest.update(repr((table_name, column_names)).encode())
        if not column_names:
            continue
        ordering = ", ".join(_quote_identifier(column) for column in column_names)
        for row in connection.execute(f"SELECT * FROM {quoted_table} ORDER BY {ordering}"):
            digest.update(repr(tuple(row)).encode())
    return digest.hexdigest()


def _backup_sqlite(path: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    descriptor, backup_name = tempfile.mkstemp(
        prefix=f"{path.name}.pre-i2-{timestamp}-",
        suffix=".bak",
        dir=path.parent,
    )
    os.close(descriptor)
    backup = Path(backup_name)
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)
    destination = sqlite3.connect(backup)
    try:
        source.execute("PRAGMA query_only=ON")
        source.execute("BEGIN")
        source_digest = _logical_database_digest(source)
        source.backup(destination)
        integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
        backup_digest = _logical_database_digest(destination)
        if integrity != "ok":
            raise MigrationSafetyError(f"Backup integrity check failed for {backup}: {integrity}")
        if backup_digest != source_digest:
            raise MigrationSafetyError(f"Backup logical-state verification failed for {backup}")
    except Exception:
        destination.close()
        source.close()
        if backup.exists():
            backup.unlink()
        raise
    else:
        destination.close()
        source.execute("ROLLBACK")
        source.close()
    return backup


def upgrade_database(database_url: str, *, adopt_legacy: bool = False) -> MigrationResult:
    """Upgrade a blank/versioned DB or explicitly adopt a verified legacy SQLite DB."""

    target = _assert_not_original_database(database_url)
    config = _config(database_url)
    head = _head_revision(config)

    if target is None:
        if adopt_legacy:
            raise MigrationSafetyError("Legacy adoption is supported only for an explicit SQLite file database.")
        command.upgrade(config, "head")
        return MigrationResult(state="blank_or_versioned", revision=head, backup_path=None)

    if not target.exists() or target.stat().st_size == 0:
        command.upgrade(config, "head")
        return MigrationResult(state="blank", revision=head, backup_path=None)

    versions = _versions(target)
    if versions is not None:
        if len(versions) != 1:
            raise MigrationSafetyError(
                f"Malformed alembic_version: expected exactly one revision row, found {len(versions)}."
            )
        current_revision = versions[0]
        if current_revision not in _known_revisions(config):
            raise MigrationSafetyError("Unknown Alembic revision: " + current_revision)
        if current_revision == head:
            return MigrationResult(state="current_head", revision=head, backup_path=None)
        backup = _backup_sqlite(target)
        command.upgrade(config, "head")
        return MigrationResult(state="versioned", revision=head, backup_path=backup)

    if not _schema_manifest(target):
        command.upgrade(config, "head")
        return MigrationResult(state="blank", revision=head, backup_path=None)

    if not adopt_legacy:
        raise MigrationSafetyError(
            "Unversioned database requires --adopt-legacy after structural compatibility verification."
        )
    validate_unversioned_legacy_sqlite(target)
    backup = _backup_sqlite(target)
    command.stamp(config, BASELINE_REVISION)
    command.upgrade(config, "head")
    return MigrationResult(state="adopted_legacy", revision=head, backup_path=backup)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True, help="Explicit target database URL; protected original DB is refused.")
    parser.add_argument("--adopt-legacy", action="store_true", help="Permit stamping only after legacy SQLite compatibility validation.")
    args = parser.parse_args()
    try:
        result = upgrade_database(args.database_url, adopt_legacy=args.adopt_legacy)
    except MigrationSafetyError as exc:
        print(f"Migration refused: {exc}", file=sys.stderr)
        return 2
    print(f"Migration complete: state={result.state} revision={result.revision}")
    if result.backup_path is not None:
        print(f"Backup: {result.backup_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
