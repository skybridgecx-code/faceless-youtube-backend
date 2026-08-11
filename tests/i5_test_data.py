from __future__ import annotations

from app.editorial.claims import compile_research_packet
from app.editorial.contracts import EditorialSeed, canonical_sha256
from app.editorial.script_compiler import compile_script_packet
from app.editorial.topic_intelligence import compile_topic_packet
from app.production.storyboard import compile_storyboard, normalize_narration

from .i4_test_data import happy_demand, happy_seed, happy_seed_payload


I5_TEST_CAMPAIGN_ID = 501
I5_TEST_PRODUCTION_PROFILE_HASH = "f" * 64


def _compile_i4_script(
    seed: EditorialSeed,
    *,
    campaign_id: int,
) -> dict[str, object]:
    topic = compile_topic_packet(
        campaign_id=campaign_id,
        seed_hash=seed.sha256(campaign_id),
        seed=seed,
        demand=happy_demand(),
        channel_niche="AI infrastructure developer tools",
        channel_audience="technical AI operators and developer teams",
    )
    research = compile_research_packet(
        campaign_id=campaign_id,
        seed=seed,
        topic_packet=topic,
        topic_packet_hash=canonical_sha256(topic),
    )
    return compile_script_packet(
        campaign_id=campaign_id,
        topic_packet=topic,
        topic_packet_hash=canonical_sha256(topic),
        research_packet=research,
        research_packet_hash=canonical_sha256(research),
    )


def happy_i4_script_packet(
    campaign_id: int = I5_TEST_CAMPAIGN_ID,
) -> dict[str, object]:
    return _compile_i4_script(happy_seed(), campaign_id=campaign_id)


def numeric_i4_script_packet(
    campaign_id: int = I5_TEST_CAMPAIGN_ID,
) -> dict[str, object]:
    payload = happy_seed_payload()
    candidate = payload["candidates"][0]  # type: ignore[index]
    claims = candidate["claims"]  # type: ignore[index]
    evidence_claim = next(
        claim for claim in claims if claim["role"] == "evidence"  # type: ignore[union-attr]
    )
    evidence_claim["assertion_text"] = (
        "The verified benchmark record contains 42 repeatable inference scenarios."
    )
    seed = EditorialSeed.model_validate(payload)
    return _compile_i4_script(seed, campaign_id=campaign_id)


def happy_storyboard_packet(
    campaign_id: int = I5_TEST_CAMPAIGN_ID,
    *,
    generated_cinematic_enabled: bool = True,
) -> dict[str, object]:
    script = happy_i4_script_packet(campaign_id)
    return compile_storyboard(
        campaign_id=campaign_id,
        script_packet=script,
        script_hash=canonical_sha256(script),
        production_profile_hash=I5_TEST_PRODUCTION_PROFILE_HASH,
        generated_cinematic_enabled=generated_cinematic_enabled,
    )


def normalized_i4_narration(script_packet: dict[str, object]) -> str:
    return " ".join(
        normalize_narration(section["narration"])
        for section in script_packet["sections"]  # type: ignore[index]
    )
