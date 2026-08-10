import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "architecture.lock.json"
ARCH_PATH = ROOT / "ARCHITECTURE.md"
AGENT_RULE_PATH = ROOT / ".agents" / "rules" / "youtube-automation-project.md"
COMMERCIAL_SUCCESS_PATH = ROOT / "COMMERCIAL_SUCCESS.md"
README_PATH = ROOT / "README.md"
REQUIREMENTS_PATH = ROOT / "requirements.txt"

EXPECTED_STAGES = [
    "topic",
    "research",
    "script",
    "storyboard",
    "media",
    "assembly",
    "machine_qa",
    "private_upload",
    "human_approval",
    "release",
]

REQUIRED_INVARIANTS = {
    "no_public_release_without_hash_bound_human_approval",
    "no_unsupported_material_claims",
    "no_unknown_rights_media",
    "no_provider_direct_state_mutation",
    "analytics_cannot_mutate_policy",
    "hard_gate_failure_cannot_be_overridden_by_soft_score",
    "accepted_artifacts_are_immutable",
    "all_retries_time_and_cost_are_bounded",
    "commercial_scores_cannot_override_truth_rights_or_safety_gates",
    "no_unconstrained_view_or_revenue_maximization",
    "sponsor_economics_cannot_influence_editorial_claims_or_conclusions",
    "production_volume_is_not_primary_optimization_target",
    "controlled_editorial_exploration_is_required",
}

REQUIRED_FORBIDDEN_V1 = {
    "microservices",
    "celery",
    "redis",
    "kafka",
    "temporal_cluster",
    "dynamic_agent_swarms",
    "self_modifying_prompts",
    "auto_public_release",
    "multi_channel_tenancy",
}


def load_lock() -> dict[str, object]:
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


def test_architecture_lock_identity_and_baseline() -> None:
    lock = load_lock()

    assert lock["schema_version"] == 2
    assert lock["product"] == "autonomous-youtube-studio"
    assert lock["architecture_status"] == "target_during_controlled_migration"
    assert lock["deployment"] == "modular-monolith"

    baseline = lock["i0_baseline"]
    assert baseline == {
        "repository": "Aatifshow33/faceless-youtube-backend",
        "branch": "youmo-clone-v2",
        "sha": "3f270573d2786762123e9d11239f65a6c4d2061a",
        "shared_historical_anchor": "5afebd93b98e6b65ccec41da036ad1ec972dc517",
    }

    historical = lock["historical_blueprint_audit"]
    assert historical["repository"] == "skybridgecx-code/faceless-youtube-backend"
    assert historical["sha"] == "20c30309d30a93bda9474c10c28f81cf237b733f"
    assert historical["status"] == "historical_repository_state_only"


def test_architecture_lock_runtime_and_safety_contract() -> None:
    lock = load_lock()

    assert lock["runtime_stages"] == EXPECTED_STAGES
    assert set(lock["hard_invariants"]) >= REQUIRED_INVARIANTS
    assert set(lock["forbidden_v1"]) >= REQUIRED_FORBIDDEN_V1


def test_commercial_success_contract() -> None:
    commercial = load_lock()["commercial_success_contract"]

    assert commercial["north_star"] == (
        "maximize_long_term_viewer_value_and_sustainable_campaign_economics_"
        "subject_to_hard_invariants"
    )
    assert commercial["primary_metric_priority"] == [
        "viewer_satisfaction",
        "watch_time_and_retention",
        "qualified_click_appeal",
        "returning_viewer_growth",
        "subscriber_conversion",
        "revenue_per_campaign",
        "production_efficiency",
    ]
    assert commercial["forbidden_autonomous_objectives"] == [
        "maximize_views_unconstrained",
        "maximize_revenue_unconstrained",
        "maximize_upload_volume",
    ]
    assert commercial["phase_requirements"] == {
        "I4": [
            "commercial_topic_ranking",
            "viewer_promise",
            "narrative_engineering",
            "narrator_editorial_identity",
        ],
        "I5": [
            "retention_oriented_visual_selection",
            "visual_mode_diversity",
            "bounded_generated_media",
            "meaningful_visual_progression",
        ],
        "I6": [
            "first_30_seconds_hook_qa",
            "multiple_distinct_packaging_concepts",
            "packaging_truth_gate",
            "thumbnail_readability_and_hierarchy",
        ],
        "I7": [
            "audience_learning",
            "campaign_unit_economics",
            "performance_snapshots",
            "analytics_nonmutation",
        ],
    }
    assert commercial["first_meaningful_public_evaluation_campaigns"] == 10
    assert commercial["controlled_exploration_required"] is True
    assert commercial["sponsors_separated_from_editorial_reasoning"] is True
    assert commercial["analytics_may_recommend_but_not_self_modify"] is True


def test_current_state_reconciliation_is_explicit() -> None:
    current = load_lock()["current_state_at_i0"]

    assert current["alembic_present"] is True
    assert current["startup_schema_mutation_present"] is True
    assert current["private_youtube_upload_path_present"] is True
    assert current["campaign_root_present"] is False
    assert current["dbos_workflow_present"] is False
    assert current["hash_bound_release_approval_present"] is False


def test_governance_documents_exist_and_old_niche_rule_is_retired() -> None:
    assert ARCH_PATH.is_file()
    assert AGENT_RULE_PATH.is_file()
    assert COMMERCIAL_SUCCESS_PATH.is_file()
    assert README_PATH.is_file()

    architecture_text = ARCH_PATH.read_text(encoding="utf-8")
    agent_text = AGENT_RULE_PATH.read_text(encoding="utf-8")
    commercial_text = COMMERCIAL_SUCCESS_PATH.read_text(encoding="utf-8")
    readme_text = README_PATH.read_text(encoding="utf-8")

    assert "Autonomous YouTube Studio" in architecture_text
    assert "Autonomous YouTube Studio" in agent_text
    assert "Autonomous YouTube Studio" in commercial_text
    assert "Autonomous YouTube Studio" in readme_text
    assert "COMMERCIAL_SUCCESS.md" in architecture_text
    assert "COMMERCIAL_SUCCESS.md" in agent_text

    assert "AI automation for local service businesses." not in agent_text
    assert "YOUTUBE_AUTOMATION_PROJECT_INSTRUCTIONS.md" not in agent_text


def test_forbidden_v1_runtime_dependencies_are_not_installed() -> None:
    requirements = REQUIREMENTS_PATH.read_text(encoding="utf-8").lower()

    forbidden_packages = (
        "celery",
        "redis",
        "kafka-python",
        "confluent-kafka",
        "temporalio",
    )

    for package in forbidden_packages:
        assert not any(
            line.strip().startswith(package)
            for line in requirements.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
