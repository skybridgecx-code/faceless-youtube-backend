from collections.abc import Generator
from typing import Any

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _apply_sqlite_video_preview_columns()
    _apply_sqlite_video_opportunity_review_columns()
    _apply_sqlite_agent_system_columns()
    _apply_sqlite_production_brief_columns()
    _apply_sqlite_visual_asset_factory_columns()
    _apply_sqlite_visual_generation_queue_columns()
    _apply_sqlite_video_performance_columns()
    _apply_sqlite_publishing_payload_columns()
    _apply_sqlite_research_columns()
    _seed_default_agents_for_existing_channels()


def _sqlite_table_exists(conn: Any, table_name: str) -> bool:
    return bool(
        conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name=:table_name"),
            {"table_name": table_name},
        ).fetchone()
    )


def _apply_sqlite_additive_columns(table_name: str, required_columns: dict[str, str]) -> None:
    with engine.begin() as conn:
        if not _sqlite_table_exists(conn, table_name):
            return

        existing_columns = {
            row[1]
            for row in conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
        }
        for column_name, alter_sql in required_columns.items():
            if column_name not in existing_columns:
                conn.execute(text(alter_sql))


def _apply_sqlite_video_preview_columns() -> None:
    if not settings.database_url.startswith("sqlite"):
        return

    required_columns = {
        "rendered_preview_path": "ALTER TABLE videos ADD COLUMN rendered_preview_path TEXT",
        "preview_rendered_at": "ALTER TABLE videos ADD COLUMN preview_rendered_at DATETIME",
        "preview_reviewed": "ALTER TABLE videos ADD COLUMN preview_reviewed BOOLEAN DEFAULT 0",
        "preview_reviewed_at": "ALTER TABLE videos ADD COLUMN preview_reviewed_at DATETIME",
    }
    _apply_sqlite_additive_columns("videos", required_columns)


def _apply_sqlite_video_opportunity_review_columns() -> None:
    if not settings.database_url.startswith("sqlite"):
        return

    required_columns = {
        "review_status": "ALTER TABLE video_opportunities ADD COLUMN review_status TEXT DEFAULT 'unreviewed'",
        "operator_notes": "ALTER TABLE video_opportunities ADD COLUMN operator_notes TEXT",
        "rejection_reason": "ALTER TABLE video_opportunities ADD COLUMN rejection_reason TEXT",
        "decision_summary": "ALTER TABLE video_opportunities ADD COLUMN decision_summary TEXT",
        "reviewed_at": "ALTER TABLE video_opportunities ADD COLUMN reviewed_at DATETIME",
    }
    _apply_sqlite_additive_columns("video_opportunities", required_columns)


def _apply_sqlite_agent_system_columns() -> None:
    if not settings.database_url.startswith("sqlite"):
        return

    _apply_sqlite_additive_columns(
        "videos",
        {
            "assigned_agent_id": "ALTER TABLE videos ADD COLUMN assigned_agent_id INTEGER",
        },
    )
    _apply_sqlite_additive_columns(
        "video_opportunities",
        {
            "assigned_agent_id": "ALTER TABLE video_opportunities ADD COLUMN assigned_agent_id INTEGER",
        },
    )
    _apply_sqlite_additive_columns(
        "executive_producer_recommendations",
        {
            "matched_agent_id": "ALTER TABLE executive_producer_recommendations ADD COLUMN matched_agent_id INTEGER",
            "matched_agent_name": "ALTER TABLE executive_producer_recommendations ADD COLUMN matched_agent_name TEXT",
            "matched_agent_lane": "ALTER TABLE executive_producer_recommendations ADD COLUMN matched_agent_lane TEXT",
        },
    )
    _apply_sqlite_additive_columns(
        "content_agents",
        {
            "lane": "ALTER TABLE content_agents ADD COLUMN lane TEXT DEFAULT ''",
            "focus": "ALTER TABLE content_agents ADD COLUMN focus TEXT DEFAULT ''",
            "monetization_focus": "ALTER TABLE content_agents ADD COLUMN monetization_focus TEXT DEFAULT ''",
            "compliance_notes": "ALTER TABLE content_agents ADD COLUMN compliance_notes TEXT DEFAULT ''",
            "production_rules": "ALTER TABLE content_agents ADD COLUMN production_rules TEXT DEFAULT ''",
            "is_active": "ALTER TABLE content_agents ADD COLUMN is_active BOOLEAN DEFAULT 1",
            "updated_at": "ALTER TABLE content_agents ADD COLUMN updated_at DATETIME",
        },
    )


def _apply_sqlite_production_brief_columns() -> None:
    if not settings.database_url.startswith("sqlite"):
        return

    _apply_sqlite_additive_columns(
        "production_briefs",
        {
            "opportunity_id": "ALTER TABLE production_briefs ADD COLUMN opportunity_id INTEGER",
            "assigned_agent_id": "ALTER TABLE production_briefs ADD COLUMN assigned_agent_id INTEGER",
            "promoted_video_id": "ALTER TABLE production_briefs ADD COLUMN promoted_video_id INTEGER",
            "status": "ALTER TABLE production_briefs ADD COLUMN status TEXT DEFAULT 'draft'",
            "topic": "ALTER TABLE production_briefs ADD COLUMN topic TEXT DEFAULT ''",
            "niche_lane": "ALTER TABLE production_briefs ADD COLUMN niche_lane TEXT DEFAULT ''",
            "target_audience": "ALTER TABLE production_briefs ADD COLUMN target_audience TEXT DEFAULT ''",
            "monetization_path": "ALTER TABLE production_briefs ADD COLUMN monetization_path TEXT DEFAULT ''",
            "title": "ALTER TABLE production_briefs ADD COLUMN title TEXT DEFAULT ''",
            "thumbnail_angle": "ALTER TABLE production_briefs ADD COLUMN thumbnail_angle TEXT DEFAULT ''",
            "hook": "ALTER TABLE production_briefs ADD COLUMN hook TEXT DEFAULT ''",
            "outline": "ALTER TABLE production_briefs ADD COLUMN outline TEXT DEFAULT ''",
            "script_plan": "ALTER TABLE production_briefs ADD COLUMN script_plan TEXT DEFAULT ''",
            "b_roll_plan": "ALTER TABLE production_briefs ADD COLUMN b_roll_plan TEXT DEFAULT ''",
            "voiceover_style": "ALTER TABLE production_briefs ADD COLUMN voiceover_style TEXT DEFAULT ''",
            "cta": "ALTER TABLE production_briefs ADD COLUMN cta TEXT DEFAULT ''",
            "compliance_notes": "ALTER TABLE production_briefs ADD COLUMN compliance_notes TEXT DEFAULT ''",
            "claims_to_verify": "ALTER TABLE production_briefs ADD COLUMN claims_to_verify TEXT DEFAULT ''",
            "operator_review_notes": "ALTER TABLE production_briefs ADD COLUMN operator_review_notes TEXT",
            "created_at": "ALTER TABLE production_briefs ADD COLUMN created_at DATETIME",
            "updated_at": "ALTER TABLE production_briefs ADD COLUMN updated_at DATETIME",
        },
    )


def _apply_sqlite_research_columns() -> None:
    if not settings.database_url.startswith("sqlite"):
        return

    _apply_sqlite_additive_columns(
        "research_runs",
        {
            "channel_id": "ALTER TABLE research_runs ADD COLUMN channel_id INTEGER",
            "assigned_agent_id": "ALTER TABLE research_runs ADD COLUMN assigned_agent_id INTEGER",
            "niche_lane": "ALTER TABLE research_runs ADD COLUMN niche_lane TEXT DEFAULT ''",
            "query": "ALTER TABLE research_runs ADD COLUMN query TEXT DEFAULT ''",
            "max_results": "ALTER TABLE research_runs ADD COLUMN max_results INTEGER DEFAULT 10",
            "status": "ALTER TABLE research_runs ADD COLUMN status TEXT DEFAULT 'pending'",
            "setup_required": "ALTER TABLE research_runs ADD COLUMN setup_required BOOLEAN DEFAULT 0",
            "setup_message": "ALTER TABLE research_runs ADD COLUMN setup_message TEXT",
            "source_video_count": "ALTER TABLE research_runs ADD COLUMN source_video_count INTEGER DEFAULT 0",
            "source_channel_count": "ALTER TABLE research_runs ADD COLUMN source_channel_count INTEGER DEFAULT 0",
            "created_at": "ALTER TABLE research_runs ADD COLUMN created_at DATETIME",
            "updated_at": "ALTER TABLE research_runs ADD COLUMN updated_at DATETIME",
        },
    )
    _apply_sqlite_additive_columns(
        "research_source_videos",
        {
            "run_id": "ALTER TABLE research_source_videos ADD COLUMN run_id INTEGER",
            "youtube_video_id": "ALTER TABLE research_source_videos ADD COLUMN youtube_video_id TEXT DEFAULT ''",
            "youtube_channel_id": "ALTER TABLE research_source_videos ADD COLUMN youtube_channel_id TEXT DEFAULT ''",
            "title": "ALTER TABLE research_source_videos ADD COLUMN title TEXT DEFAULT ''",
            "channel_title": "ALTER TABLE research_source_videos ADD COLUMN channel_title TEXT DEFAULT ''",
            "description_snippet": "ALTER TABLE research_source_videos ADD COLUMN description_snippet TEXT DEFAULT ''",
            "published_at": "ALTER TABLE research_source_videos ADD COLUMN published_at DATETIME",
            "duration": "ALTER TABLE research_source_videos ADD COLUMN duration TEXT",
            "view_count": "ALTER TABLE research_source_videos ADD COLUMN view_count INTEGER",
            "like_count": "ALTER TABLE research_source_videos ADD COLUMN like_count INTEGER",
            "comment_count": "ALTER TABLE research_source_videos ADD COLUMN comment_count INTEGER",
            "thumbnail_url": "ALTER TABLE research_source_videos ADD COLUMN thumbnail_url TEXT",
            "position": "ALTER TABLE research_source_videos ADD COLUMN position INTEGER DEFAULT 0",
            "created_at": "ALTER TABLE research_source_videos ADD COLUMN created_at DATETIME",
        },
    )
    _apply_sqlite_additive_columns(
        "research_source_channels",
        {
            "run_id": "ALTER TABLE research_source_channels ADD COLUMN run_id INTEGER",
            "youtube_channel_id": "ALTER TABLE research_source_channels ADD COLUMN youtube_channel_id TEXT DEFAULT ''",
            "title": "ALTER TABLE research_source_channels ADD COLUMN title TEXT DEFAULT ''",
            "description_snippet": "ALTER TABLE research_source_channels ADD COLUMN description_snippet TEXT DEFAULT ''",
            "subscriber_count": "ALTER TABLE research_source_channels ADD COLUMN subscriber_count INTEGER",
            "video_count": "ALTER TABLE research_source_channels ADD COLUMN video_count INTEGER",
            "view_count": "ALTER TABLE research_source_channels ADD COLUMN view_count INTEGER",
            "created_at": "ALTER TABLE research_source_channels ADD COLUMN created_at DATETIME",
        },
    )
    _apply_sqlite_additive_columns(
        "research_patterns",
        {
            "run_id": "ALTER TABLE research_patterns ADD COLUMN run_id INTEGER",
            "pattern_type": "ALTER TABLE research_patterns ADD COLUMN pattern_type TEXT DEFAULT ''",
            "label": "ALTER TABLE research_patterns ADD COLUMN label TEXT DEFAULT ''",
            "details": "ALTER TABLE research_patterns ADD COLUMN details TEXT DEFAULT ''",
            "signal_strength": "ALTER TABLE research_patterns ADD COLUMN signal_strength INTEGER DEFAULT 1",
            "created_at": "ALTER TABLE research_patterns ADD COLUMN created_at DATETIME",
        },
    )
    _apply_sqlite_additive_columns(
        "research_strategies",
        {
            "run_id": "ALTER TABLE research_strategies ADD COLUMN run_id INTEGER",
            "assigned_agent_id": "ALTER TABLE research_strategies ADD COLUMN assigned_agent_id INTEGER",
            "recommended_agent_name": "ALTER TABLE research_strategies ADD COLUMN recommended_agent_name TEXT",
            "niche_lane": "ALTER TABLE research_strategies ADD COLUMN niche_lane TEXT DEFAULT ''",
            "query": "ALTER TABLE research_strategies ADD COLUMN query TEXT DEFAULT ''",
            "trend_thesis": "ALTER TABLE research_strategies ADD COLUMN trend_thesis TEXT DEFAULT ''",
            "winning_patterns": "ALTER TABLE research_strategies ADD COLUMN winning_patterns TEXT DEFAULT ''",
            "original_video_angles": "ALTER TABLE research_strategies ADD COLUMN original_video_angles TEXT DEFAULT ''",
            "recommended_topics_json": "ALTER TABLE research_strategies ADD COLUMN recommended_topics_json TEXT DEFAULT '[]'",
            "title_directions": "ALTER TABLE research_strategies ADD COLUMN title_directions TEXT DEFAULT ''",
            "thumbnail_directions": "ALTER TABLE research_strategies ADD COLUMN thumbnail_directions TEXT DEFAULT ''",
            "hook_directions": "ALTER TABLE research_strategies ADD COLUMN hook_directions TEXT DEFAULT ''",
            "monetization_path": "ALTER TABLE research_strategies ADD COLUMN monetization_path TEXT DEFAULT ''",
            "differentiation_strategy": "ALTER TABLE research_strategies ADD COLUMN differentiation_strategy TEXT DEFAULT ''",
            "what_not_to_copy": "ALTER TABLE research_strategies ADD COLUMN what_not_to_copy TEXT DEFAULT ''",
            "compliance_risks": "ALTER TABLE research_strategies ADD COLUMN compliance_risks TEXT DEFAULT ''",
            "recommended_next_action": "ALTER TABLE research_strategies ADD COLUMN recommended_next_action TEXT DEFAULT ''",
            "top_pattern": "ALTER TABLE research_strategies ADD COLUMN top_pattern TEXT",
            "created_at": "ALTER TABLE research_strategies ADD COLUMN created_at DATETIME",
            "updated_at": "ALTER TABLE research_strategies ADD COLUMN updated_at DATETIME",
        },
    )


def _apply_sqlite_visual_asset_factory_columns() -> None:
    if not settings.database_url.startswith("sqlite"):
        return

    _apply_sqlite_additive_columns(
        "visual_asset_plans",
        {
            "video_id": "ALTER TABLE visual_asset_plans ADD COLUMN video_id INTEGER",
            "brief_id": "ALTER TABLE visual_asset_plans ADD COLUMN brief_id INTEGER",
            "source_type": "ALTER TABLE visual_asset_plans ADD COLUMN source_type TEXT DEFAULT 'video'",
            "status": "ALTER TABLE visual_asset_plans ADD COLUMN status TEXT DEFAULT 'draft'",
            "title": "ALTER TABLE visual_asset_plans ADD COLUMN title TEXT DEFAULT ''",
            "thumbnail_prompt": "ALTER TABLE visual_asset_plans ADD COLUMN thumbnail_prompt TEXT DEFAULT ''",
            "thumbnail_text": "ALTER TABLE visual_asset_plans ADD COLUMN thumbnail_text TEXT DEFAULT ''",
            "motion_style": "ALTER TABLE visual_asset_plans ADD COLUMN motion_style TEXT DEFAULT ''",
            "color_direction": "ALTER TABLE visual_asset_plans ADD COLUMN color_direction TEXT DEFAULT ''",
            "plan_notes": "ALTER TABLE visual_asset_plans ADD COLUMN plan_notes TEXT",
            "safety_notes": "ALTER TABLE visual_asset_plans ADD COLUMN safety_notes TEXT DEFAULT ''",
            "ready_marked_at": "ALTER TABLE visual_asset_plans ADD COLUMN ready_marked_at DATETIME",
            "created_at": "ALTER TABLE visual_asset_plans ADD COLUMN created_at DATETIME",
            "updated_at": "ALTER TABLE visual_asset_plans ADD COLUMN updated_at DATETIME",
        },
    )
    _apply_sqlite_additive_columns(
        "visual_scenes",
        {
            "plan_id": "ALTER TABLE visual_scenes ADD COLUMN plan_id INTEGER",
            "scene_number": "ALTER TABLE visual_scenes ADD COLUMN scene_number INTEGER DEFAULT 1",
            "scene_title": "ALTER TABLE visual_scenes ADD COLUMN scene_title TEXT DEFAULT ''",
            "narrative_beat": "ALTER TABLE visual_scenes ADD COLUMN narrative_beat TEXT DEFAULT ''",
            "on_screen_text": "ALTER TABLE visual_scenes ADD COLUMN on_screen_text TEXT DEFAULT ''",
            "image_prompt": "ALTER TABLE visual_scenes ADD COLUMN image_prompt TEXT DEFAULT ''",
            "animation_prompt": "ALTER TABLE visual_scenes ADD COLUMN animation_prompt TEXT DEFAULT ''",
            "b_roll_prompt": "ALTER TABLE visual_scenes ADD COLUMN b_roll_prompt TEXT DEFAULT ''",
            "dashboard_demo_prompt": "ALTER TABLE visual_scenes ADD COLUMN dashboard_demo_prompt TEXT DEFAULT ''",
            "safety_notes": "ALTER TABLE visual_scenes ADD COLUMN safety_notes TEXT DEFAULT ''",
            "asset_status": "ALTER TABLE visual_scenes ADD COLUMN asset_status TEXT DEFAULT 'planned'",
            "generated_asset_path": "ALTER TABLE visual_scenes ADD COLUMN generated_asset_path TEXT",
            "created_at": "ALTER TABLE visual_scenes ADD COLUMN created_at DATETIME",
            "updated_at": "ALTER TABLE visual_scenes ADD COLUMN updated_at DATETIME",
        },
    )
    _apply_sqlite_additive_columns(
        "visual_asset_prompts",
        {
            "plan_id": "ALTER TABLE visual_asset_prompts ADD COLUMN plan_id INTEGER",
            "scene_id": "ALTER TABLE visual_asset_prompts ADD COLUMN scene_id INTEGER",
            "prompt_type": "ALTER TABLE visual_asset_prompts ADD COLUMN prompt_type TEXT DEFAULT ''",
            "label": "ALTER TABLE visual_asset_prompts ADD COLUMN label TEXT DEFAULT ''",
            "prompt_text": "ALTER TABLE visual_asset_prompts ADD COLUMN prompt_text TEXT DEFAULT ''",
            "created_at": "ALTER TABLE visual_asset_prompts ADD COLUMN created_at DATETIME",
            "updated_at": "ALTER TABLE visual_asset_prompts ADD COLUMN updated_at DATETIME",
        },
    )


def _apply_sqlite_visual_generation_queue_columns() -> None:
    if not settings.database_url.startswith("sqlite"):
        return

    _apply_sqlite_additive_columns(
        "visual_generation_jobs",
        {
            "visual_asset_plan_id": "ALTER TABLE visual_generation_jobs ADD COLUMN visual_asset_plan_id INTEGER",
            "visual_scene_id": "ALTER TABLE visual_generation_jobs ADD COLUMN visual_scene_id INTEGER",
            "prompt_id": "ALTER TABLE visual_generation_jobs ADD COLUMN prompt_id INTEGER",
            "job_type": "ALTER TABLE visual_generation_jobs ADD COLUMN job_type TEXT DEFAULT ''",
            "provider": "ALTER TABLE visual_generation_jobs ADD COLUMN provider TEXT DEFAULT 'manual'",
            "status": "ALTER TABLE visual_generation_jobs ADD COLUMN status TEXT DEFAULT 'queued'",
            "prompt": "ALTER TABLE visual_generation_jobs ADD COLUMN prompt TEXT DEFAULT ''",
            "negative_prompt": "ALTER TABLE visual_generation_jobs ADD COLUMN negative_prompt TEXT",
            "provider_payload_json": "ALTER TABLE visual_generation_jobs ADD COLUMN provider_payload_json TEXT DEFAULT '{}'",
            "output_path": "ALTER TABLE visual_generation_jobs ADD COLUMN output_path TEXT",
            "failure_reason": "ALTER TABLE visual_generation_jobs ADD COLUMN failure_reason TEXT",
            "created_at": "ALTER TABLE visual_generation_jobs ADD COLUMN created_at DATETIME",
            "updated_at": "ALTER TABLE visual_generation_jobs ADD COLUMN updated_at DATETIME",
        },
    )

    _apply_sqlite_additive_columns(
        "visual_generated_assets",
        {
            "visual_asset_plan_id": "ALTER TABLE visual_generated_assets ADD COLUMN visual_asset_plan_id INTEGER",
            "visual_scene_id": "ALTER TABLE visual_generated_assets ADD COLUMN visual_scene_id INTEGER",
            "generation_job_id": "ALTER TABLE visual_generated_assets ADD COLUMN generation_job_id INTEGER",
            "asset_type": "ALTER TABLE visual_generated_assets ADD COLUMN asset_type TEXT DEFAULT ''",
            "file_path": "ALTER TABLE visual_generated_assets ADD COLUMN file_path TEXT DEFAULT ''",
            "file_exists": "ALTER TABLE visual_generated_assets ADD COLUMN file_exists BOOLEAN DEFAULT 0",
            "mime_type": "ALTER TABLE visual_generated_assets ADD COLUMN mime_type TEXT",
            "duration_seconds": "ALTER TABLE visual_generated_assets ADD COLUMN duration_seconds FLOAT",
            "width": "ALTER TABLE visual_generated_assets ADD COLUMN width INTEGER",
            "height": "ALTER TABLE visual_generated_assets ADD COLUMN height INTEGER",
            "notes": "ALTER TABLE visual_generated_assets ADD COLUMN notes TEXT",
            "created_at": "ALTER TABLE visual_generated_assets ADD COLUMN created_at DATETIME",
            "updated_at": "ALTER TABLE visual_generated_assets ADD COLUMN updated_at DATETIME",
        },
    )


def _apply_sqlite_video_performance_columns() -> None:
    if not settings.database_url.startswith("sqlite"):
        return

    _apply_sqlite_additive_columns(
        "video_performance_metrics",
        {
            "video_id": "ALTER TABLE video_performance_metrics ADD COLUMN video_id INTEGER",
            "platform": "ALTER TABLE video_performance_metrics ADD COLUMN platform TEXT DEFAULT 'youtube'",
            "published_url": "ALTER TABLE video_performance_metrics ADD COLUMN published_url TEXT",
            "impressions": "ALTER TABLE video_performance_metrics ADD COLUMN impressions INTEGER DEFAULT 0",
            "views": "ALTER TABLE video_performance_metrics ADD COLUMN views INTEGER DEFAULT 0",
            "clicks": "ALTER TABLE video_performance_metrics ADD COLUMN clicks INTEGER DEFAULT 0",
            "ctr": "ALTER TABLE video_performance_metrics ADD COLUMN ctr FLOAT",
            "average_view_duration_seconds": "ALTER TABLE video_performance_metrics ADD COLUMN average_view_duration_seconds FLOAT",
            "average_percentage_viewed": "ALTER TABLE video_performance_metrics ADD COLUMN average_percentage_viewed FLOAT",
            "watch_time_minutes": "ALTER TABLE video_performance_metrics ADD COLUMN watch_time_minutes FLOAT",
            "likes": "ALTER TABLE video_performance_metrics ADD COLUMN likes INTEGER DEFAULT 0",
            "comments": "ALTER TABLE video_performance_metrics ADD COLUMN comments INTEGER DEFAULT 0",
            "subscribers_gained": "ALTER TABLE video_performance_metrics ADD COLUMN subscribers_gained INTEGER DEFAULT 0",
            "published_at": "ALTER TABLE video_performance_metrics ADD COLUMN published_at DATETIME",
            "measured_at": "ALTER TABLE video_performance_metrics ADD COLUMN measured_at DATETIME",
            "notes": "ALTER TABLE video_performance_metrics ADD COLUMN notes TEXT",
            "created_at": "ALTER TABLE video_performance_metrics ADD COLUMN created_at DATETIME",
            "updated_at": "ALTER TABLE video_performance_metrics ADD COLUMN updated_at DATETIME",
        },
    )


def _apply_sqlite_publishing_payload_columns() -> None:
    if not settings.database_url.startswith("sqlite"):
        return

    _apply_sqlite_additive_columns(
        "publishing_payloads",
        {
            "video_id": "ALTER TABLE publishing_payloads ADD COLUMN video_id INTEGER",
            "payload_path": "ALTER TABLE publishing_payloads ADD COLUMN payload_path TEXT DEFAULT ''",
            "payload_status": "ALTER TABLE publishing_payloads ADD COLUMN payload_status TEXT DEFAULT 'draft'",
            "ready_for_manual_upload": "ALTER TABLE publishing_payloads ADD COLUMN ready_for_manual_upload BOOLEAN DEFAULT 0",
            "title": "ALTER TABLE publishing_payloads ADD COLUMN title TEXT DEFAULT ''",
            "description": "ALTER TABLE publishing_payloads ADD COLUMN description TEXT DEFAULT ''",
            "tags_json": "ALTER TABLE publishing_payloads ADD COLUMN tags_json TEXT DEFAULT '[]'",
            "category_id": "ALTER TABLE publishing_payloads ADD COLUMN category_id TEXT DEFAULT '27'",
            "privacy_status": "ALTER TABLE publishing_payloads ADD COLUMN privacy_status TEXT DEFAULT 'private'",
            "made_for_kids": "ALTER TABLE publishing_payloads ADD COLUMN made_for_kids BOOLEAN DEFAULT 0",
            "video_file_path": "ALTER TABLE publishing_payloads ADD COLUMN video_file_path TEXT",
            "thumbnail_image_path": "ALTER TABLE publishing_payloads ADD COLUMN thumbnail_image_path TEXT",
            "export_path": "ALTER TABLE publishing_payloads ADD COLUMN export_path TEXT",
            "blockers_json": "ALTER TABLE publishing_payloads ADD COLUMN blockers_json TEXT DEFAULT '[]'",
            "warnings_json": "ALTER TABLE publishing_payloads ADD COLUMN warnings_json TEXT DEFAULT '[]'",
            "manual_upload_checklist_json": "ALTER TABLE publishing_payloads ADD COLUMN manual_upload_checklist_json TEXT DEFAULT '[]'",
            "generated_at": "ALTER TABLE publishing_payloads ADD COLUMN generated_at DATETIME",
            "created_at": "ALTER TABLE publishing_payloads ADD COLUMN created_at DATETIME",
            "updated_at": "ALTER TABLE publishing_payloads ADD COLUMN updated_at DATETIME",
        },
    )


def _seed_default_agents_for_existing_channels() -> None:
    from app.models import Channel
    from app.services.agents import seed_default_agents_if_empty

    db = SessionLocal()
    try:
        channels = list(db.scalars(select(Channel)))
        for channel in channels:
            seed_default_agents_if_empty(db, channel.id)
    finally:
        db.close()
