from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.migrate_db import CANONICAL_TABLES, LEGACY_TABLES, ORIGINAL_DATABASE


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "bin" / "python"
HEAD_REVISION = "20260810_0001"
BASELINE_REVISION = "22348998243b"


def database_url(path: Path) -> str:
    return f"sqlite:///{path}"


def isolated_environment(path: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "DATABASE_URL": database_url(path),
            "OUTPUT_DIR": str(path.parent / "out"),
            "OPENAI_API_KEY": "",
            "ELEVENLABS_API_KEY": "",
            "YOUTUBE_DATA_API_KEY": "",
            "YOUTUBE_OAUTH_CLIENT_ID": "",
            "YOUTUBE_OAUTH_CLIENT_SECRET": "",
            "YOUTUBE_OAUTH_REFRESH_TOKEN": "",
        }
    )
    return environment


def run(*args: str, path: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(PYTHON), *args],
        cwd=ROOT,
        env=isolated_environment(path),
        text=True,
        capture_output=True,
        check=check,
    )


def alembic_upgrade(path: Path, revision: str = "head") -> None:
    run("-m", "alembic", "upgrade", revision, path=path)


def table_names(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        connection.close()


def columns(path: Path, table_name: str) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {row[1] for row in connection.execute(f'PRAGMA table_info("{table_name}")')}
    finally:
        connection.close()


def revision(path: Path) -> str | None:
    versions = alembic_versions(path)
    if not versions:
        return None
    if len(versions) != 1:
        raise AssertionError(f"Expected one Alembic revision row, found {versions}")
    return versions[0]


def alembic_versions(path: Path) -> list[str] | None:
    if "alembic_version" not in table_names(path):
        return None
    connection = sqlite3.connect(path)
    try:
        return [str(row[0]) for row in connection.execute("SELECT version_num FROM alembic_version ORDER BY version_num")]
    finally:
        connection.close()


def row_counts(path: Path, tables: set[str]) -> dict[str, int]:
    connection = sqlite3.connect(path)
    try:
        return {table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in sorted(tables)}
    finally:
        connection.close()


def integrity(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        return connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()


def schema_digest(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        rows = list(
            connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )
    finally:
        connection.close()
    return hashlib.sha256(repr(rows).encode()).hexdigest()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def migrate_with_utility(path: Path, *, adopt_legacy: bool = False, check: bool = True) -> subprocess.CompletedProcess[str]:
    arguments = ["scripts/migrate_db.py", "--database-url", database_url(path)]
    if adopt_legacy:
        arguments.append("--adopt-legacy")
    return run(*arguments, path=path, check=check)


def start_app(path: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    code = """
from fastapi.testclient import TestClient
from app.main import app

with TestClient(app) as client:
    assert client.get('/health').status_code == 200
    assert client.get('/app').status_code == 200
"""
    return run("-c", code, path=path, check=check)


def create_unversioned_baseline(path: Path) -> None:
    alembic_upgrade(path, BASELINE_REVISION)
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE alembic_version")
        connection.execute(
            "INSERT INTO channels (name, niche, audience, brand_voice, visual_style, created_at) "
            "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            ("Legacy channel", "legacy", "operators", "clear", "minimal"),
        )
        connection.commit()
    finally:
        connection.close()


def test_blank_sqlite_upgrades_to_head_with_complete_schema_and_no_drift(tmp_path: Path) -> None:
    database = tmp_path / "blank.db"
    alembic_upgrade(database)

    tables = table_names(database)
    assert revision(database) == HEAD_REVISION
    assert LEGACY_TABLES <= tables
    assert CANONICAL_TABLES <= tables
    assert {"campaign_id", "workflow_id", "stage", "actor"} <= columns(database, "audit_events")
    assert {
        "campaign_id",
        "publication_state",
        "privacy_status",
        "final_render_sha256",
        "metadata_sha256",
        "thumbnail_manifest_sha256",
        "disclosure_state",
        "provider_upload_id",
    } <= columns(database, "publish_records")
    assert integrity(database) == "ok"

    checked = run("-m", "alembic", "check", path=database, check=False)
    assert checked.returncode == 0, checked.stderr


def test_compatible_unversioned_legacy_database_is_verified_backed_up_and_adopted(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    create_unversioned_baseline(database)
    before = row_counts(database, LEGACY_TABLES)

    result = migrate_with_utility(database, adopt_legacy=True)

    assert "state=adopted_legacy" in result.stdout
    assert "Backup:" in result.stdout
    assert revision(database) == HEAD_REVISION
    assert row_counts(database, LEGACY_TABLES) == before
    assert row_counts(database, {"campaigns"}) == {"campaigns": 0}
    assert integrity(database) == "ok"
    assert list(tmp_path.glob("legacy.db.pre-i2-*.bak"))


def test_known_versioned_baseline_upgrades_forward_without_adoption(tmp_path: Path) -> None:
    database = tmp_path / "versioned-baseline.db"
    alembic_upgrade(database, BASELINE_REVISION)

    result = migrate_with_utility(database)

    assert "state=versioned" in result.stdout
    assert "Backup:" in result.stdout
    assert revision(database) == HEAD_REVISION
    assert CANONICAL_TABLES <= table_names(database)


def test_current_head_is_a_read_only_noop_without_backup(tmp_path: Path) -> None:
    database = tmp_path / "current-head.db"
    alembic_upgrade(database)
    before_sha = sha256(database)

    result = migrate_with_utility(database)

    assert "state=current_head" in result.stdout
    assert "Backup:" not in result.stdout
    assert sha256(database) == before_sha
    assert not list(tmp_path.glob("current-head.db.pre-i2-*.bak"))


def test_partial_legacy_database_fails_closed_without_stamping(tmp_path: Path) -> None:
    database = tmp_path / "partial.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE channels (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        connection.commit()
    finally:
        connection.close()
    before = schema_digest(database)

    result = migrate_with_utility(database, adopt_legacy=True, check=False)

    assert result.returncode == 2
    assert "missing tables" in result.stderr
    assert revision(database) is None
    assert schema_digest(database) == before


def test_missing_unprotected_baseline_column_fails_before_stamping(tmp_path: Path) -> None:
    database = tmp_path / "missing-baseline-column.db"
    create_unversioned_baseline(database)
    connection = sqlite3.connect(database)
    try:
        connection.execute("ALTER TABLE research_source_videos DROP COLUMN thumbnail_url")
        connection.commit()
    finally:
        connection.close()
    before = schema_digest(database)

    result = migrate_with_utility(database, adopt_legacy=True, check=False)

    assert result.returncode == 2
    assert "missing required columns" in result.stderr
    assert "thumbnail_url" in result.stderr
    assert alembic_versions(database) is None
    assert schema_digest(database) == before


def test_all_legacy_table_names_with_incomplete_shapes_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "all-names-incomplete.db"
    connection = sqlite3.connect(database)
    try:
        for table_name in sorted(LEGACY_TABLES):
            connection.execute(f'CREATE TABLE "{table_name}" (id INTEGER PRIMARY KEY)')
        connection.commit()
    finally:
        connection.close()
    before = schema_digest(database)

    result = migrate_with_utility(database, adopt_legacy=True, check=False)

    assert result.returncode == 2
    assert "missing required columns" in result.stderr
    assert alembic_versions(database) is None
    assert schema_digest(database) == before


def test_empty_alembic_version_table_fails_closed_without_mutation(tmp_path: Path) -> None:
    database = tmp_path / "empty-version.db"
    alembic_upgrade(database, BASELINE_REVISION)
    connection = sqlite3.connect(database)
    try:
        connection.execute("DELETE FROM alembic_version")
        connection.commit()
    finally:
        connection.close()
    before_sha = sha256(database)

    result = migrate_with_utility(database, check=False)

    assert result.returncode == 2
    assert "expected exactly one revision row, found 0" in result.stderr
    assert alembic_versions(database) == []
    assert sha256(database) == before_sha


def test_multiple_alembic_version_rows_fail_closed_without_mutation(tmp_path: Path) -> None:
    database = tmp_path / "multiple-versions.db"
    alembic_upgrade(database, BASELINE_REVISION)
    connection = sqlite3.connect(database)
    try:
        connection.execute("INSERT INTO alembic_version (version_num) VALUES (?)", (HEAD_REVISION,))
        connection.commit()
    finally:
        connection.close()
    before_sha = sha256(database)

    result = migrate_with_utility(database, check=False)

    assert result.returncode == 2
    assert "expected exactly one revision row, found 2" in result.stderr
    assert alembic_versions(database) == sorted([BASELINE_REVISION, HEAD_REVISION])
    assert sha256(database) == before_sha


def test_unknown_alembic_revision_fails_closed_without_rewrite(tmp_path: Path) -> None:
    database = tmp_path / "unknown-revision.db"
    alembic_upgrade(database, BASELINE_REVISION)
    connection = sqlite3.connect(database)
    try:
        connection.execute("UPDATE alembic_version SET version_num = 'unknown-revision'")
        connection.commit()
    finally:
        connection.close()
    before_sha = sha256(database)

    result = migrate_with_utility(database, check=False)

    assert result.returncode == 2
    assert "Unknown Alembic revision" in result.stderr
    assert revision(database) == "unknown-revision"
    assert sha256(database) == before_sha


def test_initialized_nonzero_empty_sqlite_is_migrated_as_blank(tmp_path: Path) -> None:
    database = tmp_path / "initialized-empty.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("VACUUM")
    finally:
        connection.close()
    assert database.stat().st_size > 0
    assert table_names(database) == set()

    result = migrate_with_utility(database)

    assert "state=blank" in result.stdout
    assert revision(database) == HEAD_REVISION
    assert CANONICAL_TABLES <= table_names(database)


def test_unrelated_nonzero_sqlite_is_not_treated_as_blank(tmp_path: Path) -> None:
    database = tmp_path / "unrelated.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()
    before = schema_digest(database)

    result = migrate_with_utility(database, check=False)

    assert result.returncode == 2
    assert "requires --adopt-legacy" in result.stderr
    assert schema_digest(database) == before


def test_sqlite_native_backup_captures_committed_wal_state(tmp_path: Path) -> None:
    from scripts.migrate_db import _backup_sqlite

    database = tmp_path / "wal-source.db"
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE evidence (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.commit()
        main_file_before_insert = sha256(database)
        connection.executemany(
            "INSERT INTO evidence (value) VALUES (?)",
            [("committed-one",), ("committed-two",)],
        )
        connection.commit()
        wal_path = database.with_name(f"{database.name}-wal")
        assert wal_path.is_file()
        assert wal_path.stat().st_size > 0
        assert sha256(database) == main_file_before_insert

        backup = _backup_sqlite(database)
    finally:
        connection.close()

    backup_connection = sqlite3.connect(backup)
    try:
        assert backup_connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert list(backup_connection.execute("SELECT value FROM evidence ORDER BY id")) == [
            ("committed-one",),
            ("committed-two",),
        ]
    finally:
        backup_connection.close()


def test_migrated_startup_is_read_only_and_legacy_api_operates(tmp_path: Path) -> None:
    database = tmp_path / "migrated.db"
    alembic_upgrade(database)
    before_schema = schema_digest(database)
    before_rows = row_counts(database, table_names(database) - {"alembic_version"})
    before_revision = revision(database)

    started = start_app(database)

    assert started.returncode == 0
    assert schema_digest(database) == before_schema
    assert row_counts(database, table_names(database) - {"alembic_version"}) == before_rows
    assert revision(database) == before_revision == HEAD_REVISION

    code = """
from fastapi.testclient import TestClient
from app.main import app
with TestClient(app) as client:
    response = client.post('/channels', json={'name': 'Migrated legacy API channel'})
    assert response.status_code == 200, response.text
"""
    result = run("-c", code, path=database)
    assert result.returncode == 0


def test_unmigrated_startup_fails_closed_without_schema_or_row_mutation(tmp_path: Path) -> None:
    database = tmp_path / "unmigrated.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO unrelated (value) VALUES ('evidence')")
        connection.commit()
    finally:
        connection.close()
    before_schema = schema_digest(database)
    before_rows = row_counts(database, {"unrelated"})

    result = start_app(database, check=False)

    assert result.returncode != 0
    assert "not Alembic-versioned" in result.stderr
    assert schema_digest(database) == before_schema
    assert row_counts(database, {"unrelated"}) == before_rows


def test_real_baseline_copy_adopts_without_mutating_protected_original(tmp_path: Path) -> None:
    if not ORIGINAL_DATABASE.is_file():
        pytest.skip("developer-local content_factory.db is unavailable; explicit real-copy gate is local-only")
    assert ORIGINAL_DATABASE == (ROOT / "content_factory.db").resolve()
    original_before = sha256(ORIGINAL_DATABASE)
    database = tmp_path / "content_factory-copy.db"
    shutil.copy2(ORIGINAL_DATABASE, database)
    copy_before = sha256(database)
    legacy_before = row_counts(database, LEGACY_TABLES)

    result = migrate_with_utility(database, adopt_legacy=True)

    assert "state=adopted_legacy" in result.stdout
    assert copy_before != sha256(database)
    assert integrity(database) == "ok"
    assert revision(database) == HEAD_REVISION
    assert CANONICAL_TABLES <= table_names(database)
    assert row_counts(database, LEGACY_TABLES) == legacy_before
    post_migration_schema = schema_digest(database)
    post_migration_rows = row_counts(database, table_names(database) - {"alembic_version"})
    started = start_app(database)
    assert started.returncode == 0
    assert schema_digest(database) == post_migration_schema
    assert row_counts(database, table_names(database) - {"alembic_version"}) == post_migration_rows
    assert sha256(ORIGINAL_DATABASE) == original_before


def test_protected_original_hardlink_is_refused_before_mutation(tmp_path: Path) -> None:
    if not ORIGINAL_DATABASE.is_file():
        pytest.skip("developer-local content_factory.db is unavailable; hardlink protection test is local-only")
    hardlink = tmp_path / "content_factory-hardlink.db"
    try:
        os.link(ORIGINAL_DATABASE, hardlink)
    except OSError as exc:
        pytest.skip(f"filesystem does not support the hardlink protection test: {exc}")
    original_before = sha256(ORIGINAL_DATABASE)

    result = migrate_with_utility(hardlink, adopt_legacy=True, check=False)

    assert result.returncode == 2
    assert "protected original" in result.stderr
    assert sha256(ORIGINAL_DATABASE) == original_before
    assert sha256(hardlink) == original_before


def test_cached_test_template_is_head_bound_and_rebuilt_when_corrupt(tmp_path: Path) -> None:
    from tests.db_helpers import _ensure_migrated_template

    target = tmp_path / "test_template_target.db"
    template = _ensure_migrated_template(target)
    assert HEAD_REVISION in template.name
    connection = sqlite3.connect(template)
    try:
        connection.execute("DROP TABLE channels")
        connection.commit()
    finally:
        connection.close()

    rebuilt = _ensure_migrated_template(target)

    assert rebuilt == template
    assert revision(rebuilt) == HEAD_REVISION
    assert LEGACY_TABLES <= table_names(rebuilt)
    assert CANONICAL_TABLES <= table_names(rebuilt)


def test_migration_utility_refuses_protected_original_database() -> None:
    result = migrate_with_utility(ORIGINAL_DATABASE, adopt_legacy=True, check=False)
    assert result.returncode == 2
    assert "protected original" in result.stderr
