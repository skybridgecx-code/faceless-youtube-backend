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
HEAD_REVISION = "20260811_0004"
I4_REVISION = "20260811_0003"
I3_REVISION = "20260810_0002"
I2_REVISION = "20260810_0001"
BASELINE_REVISION = "22348998243b"


def database_url(path: Path) -> str:
    return f"sqlite:///{path}"


def isolated_environment(path: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "DATABASE_URL": database_url(path),
            "DBOS_SYSTEM_DATABASE_URL": database_url(
                path.parent / f"{path.stem}-dbos-system.db"
            ),
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


def unique_index_columns(path: Path, table_name: str) -> set[tuple[str, ...]]:
    connection = sqlite3.connect(path)
    try:
        indexes: set[tuple[str, ...]] = set()
        for row in connection.execute(f'PRAGMA index_list("{table_name}")'):
            if not bool(row[2]):
                continue
            columns_for_index = tuple(
                str(info[2])
                for info in connection.execute(f'PRAGMA index_info("{row[1]}")')
            )
            indexes.add(columns_for_index)
        return indexes
    finally:
        connection.close()


def index_names(path: Path, table_name: str) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {
            str(row[1])
            for row in connection.execute(f'PRAGMA index_list("{table_name}")')
        }
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


def test_i5_settings_defaults_are_locked_without_changing_legacy_defaults() -> None:
    from app.config import Settings

    settings = Settings(_env_file=None, openai_api_key="")

    assert settings.image_generation_provider == "placeholder"
    assert settings.image_generation_model == "gpt-image-1"
    assert settings.i5_tts_provider == "openai"
    assert settings.i5_tts_model == "tts-1-hd"
    assert settings.i5_tts_fallback_model == "tts-1"
    assert settings.i5_tts_voice == "onyx"
    assert settings.i5_generated_image_provider == "openai"
    assert settings.i5_generated_image_model == "gpt-image-2"
    assert settings.i5_generated_image_size == "1280x720"
    assert settings.i5_generated_image_primary_quality == "medium"
    assert settings.i5_generated_image_fallback_quality == "low"
    assert settings.i5_video_provider == "disabled"
    assert settings.i5_video_model == "sora-2"
    assert settings.i5_allow_deprecated_sora is False
    settings.validate_startup()
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY is required"):
        settings.validate_i5_production()


@pytest.mark.parametrize(
    ("field", "invalid_value", "environment_name"),
    [
        ("i5_tts_provider", "legacy", "I5_TTS_PROVIDER"),
        ("i5_tts_model", "gpt-4o-mini-tts", "I5_TTS_MODEL"),
        ("i5_tts_fallback_model", "tts-1-hd", "I5_TTS_FALLBACK_MODEL"),
        ("i5_tts_voice", "alloy", "I5_TTS_VOICE"),
        ("i5_generated_image_provider", "legacy", "I5_GENERATED_IMAGE_PROVIDER"),
        ("i5_generated_image_model", "gpt-image-1", "I5_GENERATED_IMAGE_MODEL"),
        ("i5_generated_image_size", "1024x1024", "I5_GENERATED_IMAGE_SIZE"),
        (
            "i5_generated_image_primary_quality",
            "high",
            "I5_GENERATED_IMAGE_PRIMARY_QUALITY",
        ),
        (
            "i5_generated_image_fallback_quality",
            "medium",
            "I5_GENERATED_IMAGE_FALLBACK_QUALITY",
        ),
        ("i5_video_provider", "legacy", "I5_VIDEO_PROVIDER"),
        ("i5_video_model", "sora-2-pro", "I5_VIDEO_MODEL"),
    ],
)
def test_i5_settings_reject_provider_catalog_drift(
    field: str,
    invalid_value: str,
    environment_name: str,
) -> None:
    from app.config import Settings

    settings = Settings(_env_file=None, **{field: invalid_value})

    with pytest.raises(RuntimeError, match=environment_name):
        settings.validate_i5_configuration()


def test_i5_settings_enforce_sora_opt_in_and_image_key_precedence() -> None:
    from app.config import Settings

    blocked = Settings(
        _env_file=None,
        i5_video_provider="openai",
        i5_allow_deprecated_sora=False,
    )
    with pytest.raises(RuntimeError, match="I5_ALLOW_DEPRECATED_SORA"):
        blocked.validate_i5_configuration()

    sora_alias = Settings(
        _env_file=None,
        i5_video_provider="sora",
        i5_allow_deprecated_sora=True,
    )
    sora_alias.validate_i5_configuration()

    configured = Settings(
        _env_file=None,
        openai_api_key="tts-key",
        image_generation_api_key="image-key",
        i5_video_provider="openai",
        i5_allow_deprecated_sora=True,
    )
    configured.validate_i5_production()
    assert configured.i5_image_generation_api_key == "image-key"
    assert (
        Settings(
            _env_file=None,
            openai_api_key="shared-key",
            image_generation_api_key=" ",
        ).i5_image_generation_api_key
        == "shared-key"
    )


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
    assert "payload_json" in columns(database, "artifacts")
    assert "claim_hash" in columns(database, "claims")
    assert "production_workflow_id" in columns(database, "campaigns")
    assert "reserved_cost_microunits" in columns(database, "generation_jobs")
    assert "campaign_budget_overrides" in tables
    assert "ix_claims_claim_hash" in index_names(database, "claims")
    assert ("campaign_id", "claim_hash") in unique_index_columns(
        database, "claims"
    )
    assert integrity(database) == "ok"
    assert ("workflow_id",) in unique_index_columns(database, "campaigns")
    assert ("production_workflow_id",) in unique_index_columns(
        database, "campaigns"
    )
    assert (
        "campaign_id",
        "stage",
        "policy_version",
        "input_hash",
    ) in unique_index_columns(database, "gate_decisions")
    assert ("campaign_id", "kind", "sha256") in unique_index_columns(
        database, "artifacts"
    )
    assert (
        "campaign_id",
        "provider",
        "model",
        "input_hash",
        "attempt",
    ) in unique_index_columns(database, "generation_jobs")
    assert ("campaign_id", "override_hash") in unique_index_columns(
        database, "campaign_budget_overrides"
    )

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


def test_unversioned_legacy_with_i5_canonical_table_fails_before_adoption(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-with-i5-table.db"
    create_unversioned_baseline(database)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE campaign_budget_overrides (id INTEGER PRIMARY KEY)"
        )
        connection.commit()
    finally:
        connection.close()
    before = schema_digest(database)

    result = migrate_with_utility(database, adopt_legacy=True, check=False)

    assert result.returncode == 2
    assert "already has canonical tables" in result.stderr
    assert "campaign_budget_overrides" in result.stderr
    assert alembic_versions(database) is None
    assert schema_digest(database) == before


def test_known_versioned_baseline_upgrades_forward_without_adoption(tmp_path: Path) -> None:
    database = tmp_path / "versioned-baseline.db"
    alembic_upgrade(database, BASELINE_REVISION)

    result = migrate_with_utility(database)

    assert "state=versioned" in result.stdout
    assert "Backup:" in result.stdout
    assert revision(database) == HEAD_REVISION
    assert CANONICAL_TABLES <= table_names(database)


def test_i2_database_upgrades_to_i3_with_legacy_and_canonical_rows_preserved(
    tmp_path: Path,
) -> None:
    database = tmp_path / "i2-to-i3.db"
    alembic_upgrade(database, I2_REVISION)
    connection = sqlite3.connect(database)
    try:
        channel_id = connection.execute(
            "INSERT INTO channels "
            "(name, niche, audience, brand_voice, visual_style, created_at) "
            "VALUES ('I3 migration channel', 'test', 'test', 'test', 'test', CURRENT_TIMESTAMP)"
        ).lastrowid
        campaign_id = connection.execute(
            "INSERT INTO campaigns "
            "(channel_id, current_stage, workflow_id, risk_tier, policy_version, created_at, updated_at) "
            "VALUES (?, 'topic', NULL, 'standard', 'i3-gate-v1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (channel_id,),
        ).lastrowid
        connection.execute(
            "INSERT INTO artifacts "
            "(campaign_id, kind, uri, sha256, byte_size, mime_type, source_stage, "
            "provider_name, provider_model, prompt_template_version, provenance_json, created_at) "
            "VALUES (?, 'i2-evidence', 'stub://i2/evidence', ?, 2, 'application/json', "
            "'topic', 'stub', 'i2', 'i2', '{}', CURRENT_TIMESTAMP)",
            (campaign_id, "a" * 64),
        )
        connection.commit()
    finally:
        connection.close()

    before = row_counts(database, {"channels", "campaigns", "artifacts"})
    alembic_upgrade(database, I3_REVISION)

    assert revision(database) == I3_REVISION
    assert row_counts(database, {"channels", "campaigns", "artifacts"}) == before
    assert integrity(database) == "ok"


def test_i3_database_upgrades_to_i4_with_legacy_and_canonical_rows_preserved(
    tmp_path: Path,
) -> None:
    database = tmp_path / "i3-to-i4.db"
    alembic_upgrade(database, I3_REVISION)
    connection = sqlite3.connect(database)
    try:
        channel_id = connection.execute(
            "INSERT INTO channels "
            "(name, niche, audience, brand_voice, visual_style, created_at) "
            "VALUES ('I4 migration channel', 'engineering', 'builders', 'clear', "
            "'documentary', CURRENT_TIMESTAMP)"
        ).lastrowid
        campaign_id = connection.execute(
            "INSERT INTO campaigns "
            "(channel_id, current_stage, workflow_id, risk_tier, policy_version, created_at, updated_at) "
            "VALUES (?, 'research', 'campaign:1:i3', 'standard', 'i3-gate-v1', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (channel_id,),
        ).lastrowid
        source_id = connection.execute(
            "INSERT INTO sources "
            "(campaign_id, source_uri, publisher, retrieved_at, content_sha256, source_class, "
            "rights_status, evidence_snippet, provenance_json, created_at) "
            "VALUES (?, 'https://example.com/evidence', 'Example Publisher', CURRENT_TIMESTAMP, ?, "
            "'official', 'reference_only', 'Preserved evidence', '{\"scope\":\"snippet\"}', "
            "CURRENT_TIMESTAMP)",
            (campaign_id, "1" * 64),
        ).lastrowid
        claim_id = connection.execute(
            "INSERT INTO claims "
            "(campaign_id, assertion_text, material, state, structured_value_json, unit, created_at) "
            "VALUES (?, 'Preserved canonical claim', 1, 'VERIFIED', "
            "'{\"claim_type\":\"fact\"}', NULL, CURRENT_TIMESTAMP)",
            (campaign_id,),
        ).lastrowid
        connection.execute(
            "INSERT INTO claim_sources (claim_id, source_id, created_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP)",
            (claim_id, source_id),
        )
        artifact_id = connection.execute(
            "INSERT INTO artifacts "
            "(campaign_id, kind, uri, sha256, byte_size, mime_type, source_stage, "
            "provider_name, provider_model, prompt_template_version, provenance_json, created_at) "
            "VALUES (?, 'i3_stub_topic', 'stub://campaign/1/topic/preserved', ?, 2, "
            "'application/json', 'topic', 'stub', 'i3-deterministic-v1', "
            "'i3-stage-request-v1', '{\"preserved\":true}', CURRENT_TIMESTAMP)",
            (campaign_id, "2" * 64),
        ).lastrowid
        connection.execute(
            "INSERT INTO generation_jobs "
            "(campaign_id, scene_id, provider, model, attempt, status, input_hash, "
            "output_artifact_id, provider_job_id, usage_json, cost_microunits, error_json, "
            "created_at, completed_at) "
            "VALUES (?, NULL, 'stub', 'i3-deterministic-v1', 1, 'completed', ?, ?, "
            "'stub:preserved', '{}', 0, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (campaign_id, "3" * 64, artifact_id),
        )
        connection.execute(
            "INSERT INTO gate_decisions "
            "(campaign_id, stage, outcome, policy_version, input_hash, output_hash, "
            "reasons_json, created_at) "
            "VALUES (?, 'topic', 'PASS', 'i3-gate-v1', ?, ?, '[]', CURRENT_TIMESTAMP)",
            (campaign_id, "3" * 64, "2" * 64),
        )
        connection.commit()

        preserved_queries = {
            "channels": "SELECT id, name, niche, audience, brand_voice, visual_style FROM channels",
            "campaigns": "SELECT id, channel_id, current_stage, workflow_id, risk_tier, policy_version FROM campaigns",
            "sources": "SELECT id, campaign_id, source_uri, publisher, content_sha256, source_class, rights_status, evidence_snippet, provenance_json FROM sources",
            "claims": "SELECT id, campaign_id, assertion_text, material, state, structured_value_json, unit FROM claims",
            "claim_sources": "SELECT claim_id, source_id FROM claim_sources",
            "artifacts": "SELECT id, campaign_id, kind, uri, sha256, byte_size, mime_type, source_stage, provider_name, provider_model, prompt_template_version, provenance_json FROM artifacts",
            "generation_jobs": "SELECT id, campaign_id, provider, model, attempt, status, input_hash, output_artifact_id, provider_job_id, usage_json, cost_microunits, error_json FROM generation_jobs",
            "gate_decisions": "SELECT id, campaign_id, stage, outcome, policy_version, input_hash, output_hash, reasons_json FROM gate_decisions",
        }
        before_rows = {
            table: list(connection.execute(query))
            for table, query in preserved_queries.items()
        }
    finally:
        connection.close()

    before_counts = row_counts(database, set(preserved_queries))
    alembic_upgrade(database)

    assert revision(database) == HEAD_REVISION
    assert row_counts(database, set(preserved_queries)) == before_counts
    connection = sqlite3.connect(database)
    try:
        assert {
            table: list(connection.execute(query))
            for table, query in preserved_queries.items()
        } == before_rows
        assert connection.execute(
            "SELECT payload_json FROM artifacts WHERE id = ?", (artifact_id,)
        ).fetchone() == (None,)
        assert connection.execute(
            "SELECT claim_hash FROM claims WHERE id = ?", (claim_id,)
        ).fetchone() == (None,)
    finally:
        connection.close()
    assert integrity(database) == "ok"


def test_i4_database_upgrades_to_i5_with_all_prior_rows_preserved(
    tmp_path: Path,
) -> None:
    database = tmp_path / "i4-to-i5.db"
    alembic_upgrade(database, I4_REVISION)
    connection = sqlite3.connect(database)
    try:
        channel_id = connection.execute(
            "INSERT INTO channels "
            "(name, niche, audience, brand_voice, visual_style, created_at) "
            "VALUES ('I5 migration channel', 'engineering', 'builders', 'clear', "
            "'documentary', CURRENT_TIMESTAMP)"
        ).lastrowid
        campaign_id = connection.execute(
            "INSERT INTO campaigns "
            "(channel_id, current_stage, workflow_id, risk_tier, policy_version, created_at, updated_at) "
            "VALUES (?, 'storyboard', 'campaign:1:i4:preserved', 'standard', "
            "'i4-editorial-v1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (channel_id,),
        ).lastrowid
        source_id = connection.execute(
            "INSERT INTO sources "
            "(campaign_id, source_uri, publisher, retrieved_at, content_sha256, source_class, "
            "rights_status, evidence_snippet, provenance_json, created_at) "
            "VALUES (?, 'https://example.com/i5-preserved', 'Example Publisher', "
            "CURRENT_TIMESTAMP, ?, 'official', 'reference_only', 'Preserved I4 evidence', "
            "'{\"scope\":\"snippet\"}', CURRENT_TIMESTAMP)",
            (campaign_id, "7" * 64),
        ).lastrowid
        claim_id = connection.execute(
            "INSERT INTO claims "
            "(campaign_id, assertion_text, material, state, claim_hash, structured_value_json, "
            "unit, created_at) VALUES (?, 'Preserved I4 claim', 1, 'VERIFIED', ?, "
            "'{\"claim_type\":\"fact\"}', NULL, CURRENT_TIMESTAMP)",
            (campaign_id, "8" * 64),
        ).lastrowid
        connection.execute(
            "INSERT INTO claim_sources (claim_id, source_id, created_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP)",
            (claim_id, source_id),
        )
        artifact_id = connection.execute(
            "INSERT INTO artifacts "
            "(campaign_id, kind, uri, sha256, byte_size, mime_type, source_stage, "
            "provider_name, provider_model, prompt_template_version, payload_json, "
            "provenance_json, created_at) VALUES (?, 'i4_script', "
            "'artifact://campaign/1/i4_script/preserved', ?, 2, 'application/json', "
            "'script', 'deterministic', 'i4-editorial-v1', 'i4-script-v1', "
            "'{\"preserved\":true}', '{\"preserved\":true}', CURRENT_TIMESTAMP)",
            (campaign_id, "9" * 64),
        ).lastrowid
        connection.execute(
            "INSERT INTO generation_jobs "
            "(campaign_id, scene_id, provider, model, attempt, status, input_hash, "
            "output_artifact_id, provider_job_id, usage_json, cost_microunits, error_json, "
            "created_at, completed_at) VALUES (?, NULL, 'deterministic', "
            "'i4-script-v1', 1, 'completed', ?, ?, 'i4:preserved', '{}', 0, NULL, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (campaign_id, "a" * 64, artifact_id),
        )
        connection.execute(
            "INSERT INTO gate_decisions "
            "(campaign_id, stage, outcome, policy_version, input_hash, output_hash, "
            "reasons_json, created_at) VALUES (?, 'script', 'PASS', 'i4-editorial-v1', "
            "?, ?, '[]', CURRENT_TIMESTAMP)",
            (campaign_id, "a" * 64, "9" * 64),
        )
        connection.commit()

        preserved_queries = {
            "channels": "SELECT id, name, niche, audience, brand_voice, visual_style FROM channels",
            "campaigns": "SELECT id, channel_id, current_stage, workflow_id, risk_tier, policy_version, legacy_video_id, created_at, updated_at FROM campaigns",
            "sources": "SELECT id, campaign_id, source_uri, publisher, retrieved_at, content_sha256, source_class, rights_status, evidence_snippet, provenance_json, created_at FROM sources",
            "claims": "SELECT id, campaign_id, assertion_text, material, state, claim_hash, structured_value_json, unit, created_at FROM claims",
            "claim_sources": "SELECT claim_id, source_id, created_at FROM claim_sources",
            "artifacts": "SELECT id, campaign_id, kind, uri, sha256, byte_size, mime_type, source_stage, provider_name, provider_model, prompt_template_version, payload_json, provenance_json, created_at FROM artifacts",
            "generation_jobs": "SELECT id, campaign_id, scene_id, provider, model, attempt, status, input_hash, output_artifact_id, provider_job_id, usage_json, cost_microunits, error_json, created_at, completed_at FROM generation_jobs",
            "gate_decisions": "SELECT id, campaign_id, stage, outcome, policy_version, input_hash, output_hash, reasons_json, created_at FROM gate_decisions",
        }
        before_rows = {
            table: list(connection.execute(query))
            for table, query in preserved_queries.items()
        }
    finally:
        connection.close()

    alembic_upgrade(database)

    assert revision(database) == HEAD_REVISION
    connection = sqlite3.connect(database)
    try:
        assert {
            table: list(connection.execute(query))
            for table, query in preserved_queries.items()
        } == before_rows
        assert connection.execute(
            "SELECT production_workflow_id FROM campaigns WHERE id = ?",
            (campaign_id,),
        ).fetchone() == (None,)
        assert connection.execute(
            "SELECT reserved_cost_microunits FROM generation_jobs WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone() == (None,)
        assert connection.execute(
            "SELECT COUNT(*) FROM campaign_budget_overrides"
        ).fetchone() == (0,)
    finally:
        connection.close()
    assert integrity(database) == "ok"


def test_i5_production_and_budget_constraints_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "i5-constraints.db"
    alembic_upgrade(database)
    connection = sqlite3.connect(database)
    try:
        channel_id = connection.execute(
            "INSERT INTO channels "
            "(name, niche, audience, brand_voice, visual_style, created_at) "
            "VALUES ('I5 constraints channel', 'test', 'test', 'test', 'test', "
            "CURRENT_TIMESTAMP)"
        ).lastrowid
        campaign_ids = [
            connection.execute(
                "INSERT INTO campaigns "
                "(channel_id, current_stage, workflow_id, production_workflow_id, "
                "risk_tier, policy_version, created_at, updated_at) VALUES (?, "
                "'storyboard', NULL, ?, 'standard', 'i4-editorial-v1', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (channel_id, production_workflow_id),
            ).lastrowid
            for production_workflow_id in ("campaign:1:i5:unique", None)
        ]
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE campaigns SET production_workflow_id = ? WHERE id = ?",
                ("campaign:1:i5:unique", campaign_ids[1]),
            )
        connection.rollback()

        override_hash = "b" * 64
        valid_values = (
            campaign_ids[0],
            "i5-budget-v1",
            35_000_000,
            40_000_000,
            "owner@example.test",
            "Authorize the required narration budget",
            override_hash,
        )
        insert_override = (
            "INSERT INTO campaign_budget_overrides "
            "(campaign_id, policy_version, previous_authorized_cap_microunits, "
            "new_authorized_cap_microunits, actor, reason, override_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)"
        )
        connection.execute(insert_override, valid_values)
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(insert_override, valid_values)
        connection.rollback()

        connection.execute(
            insert_override,
            (campaign_ids[1], *valid_values[1:]),
        )
        connection.commit()

        invalid_values = (
            (*valid_values[:3], 35_000_000, *valid_values[4:6], "c" * 64),
            (*valid_values[:4], "   ", valid_values[5], "d" * 64),
            (*valid_values[:5], "   ", "e" * 64),
            (*valid_values[:6], "short-hash"),
            (
                valid_values[0],
                valid_values[1],
                -1,
                valid_values[3],
                *valid_values[4:6],
                "f" * 64,
            ),
        )
        for invalid in invalid_values:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(insert_override, invalid)
            connection.rollback()
    finally:
        connection.close()

    assert integrity(database) == "ok"


def test_i4_claim_hash_indexes_enforce_campaign_scoped_replay_identity(
    tmp_path: Path,
) -> None:
    database = tmp_path / "i4-claim-constraints.db"
    alembic_upgrade(database)
    connection = sqlite3.connect(database)
    try:
        channel_id = connection.execute(
            "INSERT INTO channels "
            "(name, niche, audience, brand_voice, visual_style, created_at) "
            "VALUES ('I4 claim constraints', 'test', 'test', 'test', 'test', CURRENT_TIMESTAMP)"
        ).lastrowid
        campaign_ids = [
            connection.execute(
                "INSERT INTO campaigns "
                "(channel_id, current_stage, workflow_id, risk_tier, policy_version, created_at, updated_at) "
                "VALUES (?, 'topic', NULL, 'standard', 'i4-editorial-v1', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (channel_id,),
            ).lastrowid
            for _ in range(2)
        ]
        claim_hash = "4" * 64
        connection.execute(
            "INSERT INTO claims "
            "(campaign_id, assertion_text, material, state, claim_hash, structured_value_json, "
            "unit, created_at) VALUES (?, 'First identity', 1, 'VERIFIED', ?, '{}', NULL, "
            "CURRENT_TIMESTAMP)",
            (campaign_ids[0], claim_hash),
        )
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO claims "
                "(campaign_id, assertion_text, material, state, claim_hash, "
                "structured_value_json, unit, created_at) "
                "VALUES (?, 'Conflicting identity', 1, 'REJECTED', ?, '{}', NULL, "
                "CURRENT_TIMESTAMP)",
                (campaign_ids[0], claim_hash),
            )
        connection.rollback()

        connection.execute(
            "INSERT INTO claims "
            "(campaign_id, assertion_text, material, state, claim_hash, structured_value_json, "
            "unit, created_at) VALUES (?, 'Other campaign', 1, 'VERIFIED', ?, '{}', NULL, "
            "CURRENT_TIMESTAMP)",
            (campaign_ids[1], claim_hash),
        )
        connection.executemany(
            "INSERT INTO claims "
            "(campaign_id, assertion_text, material, state, claim_hash, structured_value_json, "
            "unit, created_at) VALUES (?, ?, 1, 'ESTIMATE', NULL, '{}', NULL, "
            "CURRENT_TIMESTAMP)",
            [
                (campaign_ids[0], "Legacy null identity one"),
                (campaign_ids[0], "Legacy null identity two"),
            ],
        )
        connection.commit()

        assert connection.execute("SELECT COUNT(*) FROM claims").fetchone() == (4,)
    finally:
        connection.close()

    assert "ix_claims_claim_hash" in index_names(database, "claims")
    assert ("campaign_id", "claim_hash") in unique_index_columns(
        database, "claims"
    )
    assert integrity(database) == "ok"


def test_i3_replay_identity_constraints_reject_duplicates(tmp_path: Path) -> None:
    database = tmp_path / "i3-constraints.db"
    alembic_upgrade(database, I3_REVISION)
    connection = sqlite3.connect(database)
    try:
        channel_id = connection.execute(
            "INSERT INTO channels "
            "(name, niche, audience, brand_voice, visual_style, created_at) "
            "VALUES ('I3 constraints channel', 'test', 'test', 'test', 'test', CURRENT_TIMESTAMP)"
        ).lastrowid
        campaign_id = connection.execute(
            "INSERT INTO campaigns "
            "(channel_id, current_stage, workflow_id, risk_tier, policy_version, created_at, updated_at) "
            "VALUES (?, 'topic', 'campaign:1:i3', 'standard', 'i3-gate-v1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (channel_id,),
        ).lastrowid
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO campaigns "
                "(channel_id, current_stage, workflow_id, risk_tier, policy_version, created_at, updated_at) "
                "VALUES (?, 'topic', 'campaign:1:i3', 'standard', 'i3-gate-v1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (channel_id,),
            )
        connection.rollback()

        artifact_values = (
            campaign_id,
            "i3_stub_topic",
            "stub://campaign/1/topic/hash",
            "b" * 64,
        )
        connection.execute(
            "INSERT INTO artifacts "
            "(campaign_id, kind, uri, sha256, byte_size, mime_type, source_stage, "
            "provider_name, provider_model, prompt_template_version, provenance_json, created_at) "
            "VALUES (?, ?, ?, ?, 2, 'application/json', 'topic', 'stub', "
            "'i3-deterministic-v1', 'i3-stage-request-v1', '{}', CURRENT_TIMESTAMP)",
            artifact_values,
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO artifacts "
                "(campaign_id, kind, uri, sha256, byte_size, mime_type, source_stage, "
                "provider_name, provider_model, prompt_template_version, provenance_json, created_at) "
                "VALUES (?, ?, ?, ?, 2, 'application/json', 'topic', 'stub', "
                "'i3-deterministic-v1', 'i3-stage-request-v1', '{}', CURRENT_TIMESTAMP)",
                artifact_values,
            )
        connection.rollback()

        gate_values = (campaign_id, "topic", "i3-gate-v1", "c" * 64)
        connection.execute(
            "INSERT INTO gate_decisions "
            "(campaign_id, stage, outcome, policy_version, input_hash, output_hash, reasons_json, created_at) "
            "VALUES (?, ?, 'PASS', ?, ?, ?, '[]', CURRENT_TIMESTAMP)",
            (*gate_values, "d" * 64),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO gate_decisions "
                "(campaign_id, stage, outcome, policy_version, input_hash, output_hash, reasons_json, created_at) "
                "VALUES (?, ?, 'FAIL', ?, ?, NULL, '[\"conflict\"]', CURRENT_TIMESTAMP)",
                gate_values,
            )
        connection.rollback()

        job_values = (
            campaign_id,
            "stub",
            "i3-deterministic-v1",
            1,
            "e" * 64,
        )
        connection.execute(
            "INSERT INTO generation_jobs "
            "(campaign_id, scene_id, provider, model, attempt, status, input_hash, "
            "output_artifact_id, provider_job_id, usage_json, cost_microunits, error_json, "
            "created_at, completed_at) "
            "VALUES (?, NULL, ?, ?, ?, 'completed', ?, NULL, 'stub:job', '{}', 0, NULL, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            job_values,
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO generation_jobs "
                "(campaign_id, scene_id, provider, model, attempt, status, input_hash, "
                "output_artifact_id, provider_job_id, usage_json, cost_microunits, error_json, "
                "created_at, completed_at) "
                "VALUES (?, NULL, ?, ?, ?, 'failed', ?, NULL, 'stub:conflict', '{}', 0, NULL, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                job_values,
            )
        connection.rollback()
    finally:
        connection.close()


def test_i3_downgrade_removes_only_replay_indexes(tmp_path: Path) -> None:
    database = tmp_path / "i3-downgrade.db"
    alembic_upgrade(database, I3_REVISION)
    before_tables = table_names(database)

    run("-m", "alembic", "downgrade", I2_REVISION, path=database)

    assert revision(database) == I2_REVISION
    assert table_names(database) == before_tables
    assert ("workflow_id",) not in unique_index_columns(database, "campaigns")
    assert (
        "campaign_id",
        "stage",
        "policy_version",
        "input_hash",
    ) not in unique_index_columns(database, "gate_decisions")
    assert integrity(database) == "ok"


def test_i4_downgrade_removes_only_i4_columns_and_indexes(tmp_path: Path) -> None:
    database = tmp_path / "i4-downgrade.db"
    alembic_upgrade(database, I4_REVISION)
    connection = sqlite3.connect(database)
    try:
        channel_id = connection.execute(
            "INSERT INTO channels "
            "(name, niche, audience, brand_voice, visual_style, created_at) "
            "VALUES ('I4 downgrade channel', 'test', 'test', 'test', 'test', CURRENT_TIMESTAMP)"
        ).lastrowid
        campaign_id = connection.execute(
            "INSERT INTO campaigns "
            "(channel_id, current_stage, workflow_id, risk_tier, policy_version, created_at, updated_at) "
            "VALUES (?, 'topic', NULL, 'standard', 'i4-editorial-v1', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (channel_id,),
        ).lastrowid
        connection.execute(
            "INSERT INTO artifacts "
            "(campaign_id, kind, uri, sha256, byte_size, mime_type, source_stage, provider_name, "
            "provider_model, prompt_template_version, payload_json, provenance_json, created_at) "
            "VALUES (?, 'i4_editorial_seed', 'artifact://campaign/1/i4_editorial_seed/hash', ?, "
            "2, 'application/json', 'topic', 'deterministic', 'i4-editorial-v1', "
            "'i4-editorial-seed-v1', '{}', '{}', CURRENT_TIMESTAMP)",
            (campaign_id, "5" * 64),
        )
        connection.execute(
            "INSERT INTO claims "
            "(campaign_id, assertion_text, material, state, claim_hash, structured_value_json, "
            "unit, created_at) VALUES (?, 'I4 downgrade claim', 1, 'VERIFIED', ?, '{}', NULL, "
            "CURRENT_TIMESTAMP)",
            (campaign_id, "6" * 64),
        )
        connection.commit()
    finally:
        connection.close()

    preserved_tables = {"channels", "campaigns", "artifacts", "claims"}
    before_tables = table_names(database)
    before_counts = row_counts(database, preserved_tables)

    run("-m", "alembic", "downgrade", I3_REVISION, path=database)

    assert revision(database) == I3_REVISION
    assert table_names(database) == before_tables
    assert row_counts(database, preserved_tables) == before_counts
    assert "payload_json" not in columns(database, "artifacts")
    assert "claim_hash" not in columns(database, "claims")
    assert "ix_claims_claim_hash" not in index_names(database, "claims")
    assert "uq_claims_replay_identity" not in index_names(database, "claims")
    assert ("workflow_id",) in unique_index_columns(database, "campaigns")
    assert (
        "campaign_id",
        "stage",
        "policy_version",
        "input_hash",
    ) in unique_index_columns(database, "gate_decisions")
    assert ("campaign_id", "kind", "sha256") in unique_index_columns(
        database, "artifacts"
    )
    assert (
        "campaign_id",
        "provider",
        "model",
        "input_hash",
        "attempt",
    ) in unique_index_columns(database, "generation_jobs")
    assert integrity(database) == "ok"


def test_i5_downgrade_removes_only_i5_additions(tmp_path: Path) -> None:
    database = tmp_path / "i5-downgrade.db"
    alembic_upgrade(database)
    connection = sqlite3.connect(database)
    try:
        channel_id = connection.execute(
            "INSERT INTO channels "
            "(name, niche, audience, brand_voice, visual_style, created_at) "
            "VALUES ('I5 downgrade channel', 'test', 'test', 'test', 'test', "
            "CURRENT_TIMESTAMP)"
        ).lastrowid
        campaign_id = connection.execute(
            "INSERT INTO campaigns "
            "(channel_id, current_stage, workflow_id, production_workflow_id, risk_tier, "
            "policy_version, created_at, updated_at) VALUES (?, 'storyboard', "
            "'campaign:1:i4:downgrade', 'campaign:1:i5:downgrade', 'standard', "
            "'i4-editorial-v1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (channel_id,),
        ).lastrowid
        connection.execute(
            "INSERT INTO generation_jobs "
            "(campaign_id, scene_id, provider, model, attempt, status, input_hash, "
            "output_artifact_id, provider_job_id, usage_json, cost_microunits, "
            "reserved_cost_microunits, error_json, created_at, completed_at) "
            "VALUES (?, NULL, 'openai', 'tts-1-hd', 1, 'reserved', ?, NULL, NULL, "
            "NULL, NULL, 1200, NULL, CURRENT_TIMESTAMP, NULL)",
            (campaign_id, "1" * 64),
        )
        connection.execute(
            "INSERT INTO campaign_budget_overrides "
            "(campaign_id, policy_version, previous_authorized_cap_microunits, "
            "new_authorized_cap_microunits, actor, reason, override_hash, created_at) "
            "VALUES (?, 'i5-budget-v1', 35000000, 40000000, 'owner', "
            "'Required production budget', ?, CURRENT_TIMESTAMP)",
            (campaign_id, "2" * 64),
        )
        connection.commit()
        preserved_campaign = connection.execute(
            "SELECT id, channel_id, current_stage, workflow_id, risk_tier, policy_version, "
            "legacy_video_id, created_at, updated_at FROM campaigns WHERE id = ?",
            (campaign_id,),
        ).fetchone()
        preserved_job = connection.execute(
            "SELECT id, campaign_id, scene_id, provider, model, attempt, status, input_hash, "
            "output_artifact_id, provider_job_id, usage_json, cost_microunits, error_json, "
            "created_at, completed_at FROM generation_jobs WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()
    finally:
        connection.close()

    before_tables = table_names(database)
    run("-m", "alembic", "downgrade", I4_REVISION, path=database)

    assert revision(database) == I4_REVISION
    assert table_names(database) == before_tables - {"campaign_budget_overrides"}
    assert "production_workflow_id" not in columns(database, "campaigns")
    assert "reserved_cost_microunits" not in columns(database, "generation_jobs")
    assert "uq_campaigns_production_workflow_id" not in index_names(
        database, "campaigns"
    )
    assert "payload_json" in columns(database, "artifacts")
    assert "claim_hash" in columns(database, "claims")
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT id, channel_id, current_stage, workflow_id, risk_tier, policy_version, "
            "legacy_video_id, created_at, updated_at FROM campaigns WHERE id = ?",
            (campaign_id,),
        ).fetchone() == preserved_campaign
        assert connection.execute(
            "SELECT id, campaign_id, scene_id, provider, model, attempt, status, input_hash, "
            "output_artifact_id, provider_job_id, usage_json, cost_microunits, error_json, "
            "created_at, completed_at FROM generation_jobs WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone() == preserved_job
    finally:
        connection.close()
    assert integrity(database) == "ok"


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
    dbos_system_database = database.parent / f"{database.stem}-dbos-system.db"

    result = start_app(database, check=False)

    assert result.returncode != 0
    assert "not Alembic-versioned" in result.stderr
    assert schema_digest(database) == before_schema
    assert row_counts(database, {"unrelated"}) == before_rows
    assert not dbos_system_database.exists()


def test_stale_i3_startup_fails_before_dbos_or_application_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "stale-i3.db"
    alembic_upgrade(database, I3_REVISION)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "INSERT INTO channels "
            "(name, niche, audience, brand_voice, visual_style, created_at) "
            "VALUES ('Stale I3 channel', 'test', 'test', 'test', 'test', CURRENT_TIMESTAMP)"
        )
        connection.commit()
    finally:
        connection.close()
    before_sha = sha256(database)
    before_schema = schema_digest(database)
    before_rows = row_counts(database, table_names(database) - {"alembic_version"})
    dbos_system_database = database.parent / f"{database.stem}-dbos-system.db"

    result = start_app(database, check=False)

    assert result.returncode != 0
    assert (
        f"Database revision ['{I3_REVISION}'] is not the expected head {HEAD_REVISION}"
        in result.stderr
    )
    assert revision(database) == I3_REVISION
    assert sha256(database) == before_sha
    assert schema_digest(database) == before_schema
    assert row_counts(database, table_names(database) - {"alembic_version"}) == before_rows
    assert not dbos_system_database.exists()


def test_stale_i4_startup_fails_before_dbos_or_application_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "stale-i4.db"
    alembic_upgrade(database, I4_REVISION)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "INSERT INTO channels "
            "(name, niche, audience, brand_voice, visual_style, created_at) "
            "VALUES ('Stale I4 channel', 'test', 'test', 'test', 'test', "
            "CURRENT_TIMESTAMP)"
        )
        connection.commit()
    finally:
        connection.close()
    before_sha = sha256(database)
    before_schema = schema_digest(database)
    before_rows = row_counts(database, table_names(database) - {"alembic_version"})
    dbos_system_database = database.parent / f"{database.stem}-dbos-system.db"

    result = start_app(database, check=False)

    assert result.returncode != 0
    assert (
        f"Database revision ['{I4_REVISION}'] is not the expected head {HEAD_REVISION}"
        in result.stderr
    )
    assert revision(database) == I4_REVISION
    assert sha256(database) == before_sha
    assert schema_digest(database) == before_schema
    assert row_counts(database, table_names(database) - {"alembic_version"}) == before_rows
    assert not dbos_system_database.exists()


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
