from __future__ import annotations

from copy import deepcopy

from app.editorial.contracts import DemandSnapshot, EditorialSeed


def happy_seed_payload() -> dict[str, object]:
    return {
        "candidates": [
            {
                "candidate_key": "open-inference-stacks",
                "topic": "Open inference stacks for technical AI teams",
                "angle": "How local inference stacks change practical deployment choices",
                "target_viewer": "technical AI operators and developer teams",
                "viewer_promise": (
                    "A source-bound explanation of where open inference stacks help, "
                    "where they still create friction, and which signals matter next."
                ),
                "core_question": (
                    "How are open inference stacks changing deployment choices for "
                    "technical AI teams?"
                ),
                "why_now": (
                    "Public benchmarks, documentation, and developer tooling now make "
                    "the tradeoffs easier to compare."
                ),
                "stakes": (
                    "Architecture choices affect iteration speed, operational control, "
                    "portability, debugging effort, and long-term maintenance."
                ),
                "novelty": (
                    "The episode connects benchmark evidence with the operational "
                    "tradeoffs that developer teams face after a model demo."
                ),
                "broad_interest_bridge": (
                    "The same control-versus-convenience tradeoff appears whenever "
                    "important software moves from a hosted service into local tools."
                ),
                "expected_takeaway": (
                    "Viewers leave with a bounded framework for comparing open inference "
                    "stacks without treating benchmarks as a universal verdict."
                ),
                "visual_modes": [
                    "DIAGRAM",
                    "DATA_VISUALIZATION",
                    "CODE",
                    "SCREENSHOT",
                ],
                "shelf_life": "evergreen",
                "sponsor_categories": ["developer tools", "AI infrastructure"],
                "sensitivity_tags": [],
                "evidence_sources": [
                    {
                        "source_key": "official-docs",
                        "source_uri": "https://docs.vllm.ai/en/latest/",
                        "publisher": "vLLM Project",
                        "source_class": "official",
                        "evidence_snippet": (
                            "The official documentation describes an open inference and "
                            "serving engine with documented deployment interfaces."
                        ),
                    },
                    {
                        "source_key": "primary-benchmark",
                        "source_uri": "https://mlcommons.org/benchmarks/inference-datacenter/",
                        "publisher": "MLCommons",
                        "source_class": "primary",
                        "evidence_snippet": (
                            "The benchmark program documents repeatable inference "
                            "measurement scenarios and submitted system results."
                        ),
                    },
                    {
                        "source_key": "secondary-analysis-a",
                        "source_uri": "https://www.wired.com/story/open-source-ai-infrastructure/",
                        "publisher": "Wired",
                        "source_class": "reputable_secondary",
                        "evidence_snippet": (
                            "Independent reporting describes growing interest in open AI "
                            "infrastructure and the operational tradeoffs around it."
                        ),
                    },
                    {
                        "source_key": "secondary-analysis-b",
                        "source_uri": "https://arstechnica.com/ai/2026/01/open-inference-tools/",
                        "publisher": "Ars Technica",
                        "source_class": "reputable_secondary",
                        "evidence_snippet": (
                            "Independent technical reporting compares deployment control "
                            "with the setup burden of local inference tools."
                        ),
                    },
                ],
                "claims": [
                    {
                        "assertion_text": (
                            "Open inference projects publish documented interfaces for "
                            "serving compatible language models."
                        ),
                        "material": True,
                        "claim_type": "fact",
                        "role": "context",
                        "source_keys": ["official-docs"],
                        "attribution": None,
                        "assumptions": [],
                    },
                    {
                        "assertion_text": (
                            "Public inference benchmarks define repeatable scenarios for "
                            "comparing submitted systems."
                        ),
                        "material": True,
                        "claim_type": "fact",
                        "role": "evidence",
                        "source_keys": ["primary-benchmark"],
                        "attribution": None,
                        "assumptions": [],
                    },
                    {
                        "assertion_text": (
                            "Independent reporting describes both increased deployment "
                            "control and additional operational setup work."
                        ),
                        "material": True,
                        "claim_type": "fact",
                        "role": "stakes",
                        "source_keys": [
                            "secondary-analysis-a",
                            "secondary-analysis-b",
                        ],
                        "attribution": None,
                        "assumptions": [],
                    },
                    {
                        "assertion_text": (
                            "The project presents continuous batching as one technique for "
                            "improving serving efficiency."
                        ),
                        "material": True,
                        "claim_type": "attributed_claim",
                        "role": "counterpoint",
                        "source_keys": ["official-docs"],
                        "attribution": "vLLM Project",
                        "assumptions": [],
                    },
                    {
                        "assertion_text": (
                            "A representative team could shorten some model-serving "
                            "experiments by standardizing one benchmarked runtime."
                        ),
                        "material": True,
                        "claim_type": "estimate",
                        "role": "uncertainty",
                        "source_keys": ["primary-benchmark"],
                        "attribution": None,
                        "assumptions": [
                            "the workload matches a published scenario",
                            "the team measures the same serving constraints",
                        ],
                    },
                    {
                        "assertion_text": (
                            "Operational clarity may matter more than headline benchmark "
                            "rankings for many small developer teams."
                        ),
                        "material": True,
                        "claim_type": "opinion",
                        "role": "outlook",
                        "source_keys": [],
                        "attribution": None,
                        "assumptions": [],
                    },
                ],
            }
        ]
    }


def happy_seed() -> EditorialSeed:
    return EditorialSeed.model_validate(happy_seed_payload())


def happy_demand_payload() -> dict[str, object]:
    videos: list[dict[str, object]] = []
    for index in range(10):
        videos.append(
            {
                "video_id": f"open-inference-{index}",
                "title": f"Open inference deployment guide {index}",
                "channel_id": f"technical-channel-{index}",
                "channel_title": f"Technical Channel {index}",
                "published_at": f"2026-08-{index + 1:02d}T12:00:00Z",
                "view_count": 900_000 + (index * 10_000),
                "like_count": 72_000 + (index * 500),
                "comment_count": 4_500 + (index * 50),
                "channel_subscriber_count": 250_000 + (index * 1_000),
            }
        )
    return {
        "contract_version": "i4-youtube-demand-v1",
        "retrieved_at": "2026-08-11T12:00:00Z",
        "candidates": [
            {
                "candidate_key": "open-inference-stacks",
                "query": "Open inference stacks for technical AI teams",
                "videos": videos,
            }
        ],
    }


def happy_demand() -> DemandSnapshot:
    return DemandSnapshot.model_validate(happy_demand_payload())


def changed_seed_payload() -> dict[str, object]:
    payload = deepcopy(happy_seed_payload())
    candidates = payload["candidates"]
    assert isinstance(candidates, list)
    candidate = candidates[0]
    assert isinstance(candidate, dict)
    candidate["angle"] = (
        "How open inference stacks alter debugging and deployment responsibilities"
    )
    return payload
