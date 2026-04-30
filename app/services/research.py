from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.services.opportunity_intake import normalize_text


class ResearchSetupRequiredError(Exception):
    pass


class ResearchFetchError(Exception):
    pass


@dataclass(frozen=True)
class SourceVideo:
    youtube_video_id: str
    youtube_channel_id: str
    title: str
    channel_title: str
    description: str
    published_at: datetime | None
    duration: str | None
    view_count: int | None
    like_count: int | None
    comment_count: int | None
    thumbnail_url: str | None


@dataclass(frozen=True)
class SourceChannel:
    youtube_channel_id: str
    title: str
    description: str
    subscriber_count: int | None
    video_count: int | None
    view_count: int | None


@dataclass(frozen=True)
class PatternResult:
    pattern_type: str
    label: str
    details: str
    signal_strength: int


@dataclass(frozen=True)
class StrategyResult:
    trend_thesis: str
    winning_patterns: str
    original_video_angles: str
    recommended_topics: list[str]
    title_directions: str
    thumbnail_directions: str
    hook_directions: str
    monetization_path: str
    differentiation_strategy: str
    what_not_to_copy: str
    compliance_risks: str
    recommended_next_action: str
    top_pattern: str


TITLE_PATTERN_RULES: tuple[tuple[str, str], ...] = (
    ("^how to\\b", "How-to framing"),
    ("\\b(best|top)\\b", "Curated picks framing"),
    ("\\b(vs|versus|comparison|compare)\\b", "Comparison framing"),
    ("\\b(checklist|framework|system|workflow)\\b", "System/checklist framing"),
    ("\\b(mistakes|avoid|wrong)\\b", "Mistake-avoidance framing"),
)

FORMAT_RULES: tuple[tuple[str, str], ...] = (
    ("\\bvs\\b|\\bcomparison\\b|\\bcompare\\b", "Comparison format"),
    ("\\bhow to\\b|\\btutorial\\b|\\bstep by step\\b", "Tutorial format"),
    ("\\bcase study\\b|\\bbreakdown\\b", "Breakdown format"),
    ("\\bchecklist\\b|\\bsystem\\b|\\bworkflow\\b", "Checklist/system format"),
)

PAIN_POINT_RULES: tuple[tuple[str, str], ...] = (
    ("missed calls|slow follow up|lead leakage", "Lead response speed and missed-intake pain"),
    ("manual|spreadsheet|chaos|disorganized", "Manual operations and fragmented workflows"),
    ("tool fatigue|too many tools|stack", "Tool-stack complexity and decision fatigue"),
    ("content consistency|burnout|time", "Creator consistency and execution bandwidth pain"),
)

MONETIZATION_CLUES: tuple[tuple[str, str], ...] = (
    ("affiliate|tool", "Affiliate software alignment"),
    ("audit|consult|service", "Consulting/audit lead path"),
    ("template|prompt pack|download", "Template and digital product path"),
    ("sponsor|partnership", "Sponsorship-fit signal"),
)

COMPLIANCE_FLAG_RULES: tuple[tuple[str, str], ...] = (
    ("guarantee|guaranteed|make money fast|get rich", "Income/guarantee language risk"),
    ("client results|case study result|10x", "Unsupported outcomes risk"),
    ("this company uses|used by .* brand", "Real-company claim verification risk"),
)

LANE_TOPIC_BANK: dict[str, tuple[str, ...]] = {
    "ai tool breakdowns": (
        "AI tool stack teardown for a single operator workflow",
        "Operator-first AI tool comparison with compliance checkpoints",
        "How to evaluate AI tools without hype claims",
    ),
    "ai business automation": (
        "Small business AI workflow map for lead response",
        "Automation handoff system for local service operations",
        "Operator SOP for AI-assisted customer intake",
    ),
    "ai side hustles business model breakdowns": (
        "Realistic AI service model breakdown with risk controls",
        "Side-hustle workflow plan without income guarantees",
        "Operator audit offer buildout using AI workflows",
    ),
    "faceless youtube creator automation": (
        "Faceless creator pipeline from opportunity to payload",
        "YouTube content ops checklist for creator automation teams",
        "Draft preview review system for faceless channels",
    ),
    "ecommerce ai": (
        "Ecommerce AI operator workflow for product research triage",
        "Store operations automation plan with measurable checkpoints",
        "AI-assisted merchandising workflow without inflated claims",
    ),
    "local business ai automation": (
        "Local business AI call-flow design for appointment capture",
        "Lead intake automation board for service operators",
        "Manual approval-first local automation workflow",
    ),
    "career productivity ai": (
        "Weekly operator planning system with AI productivity assistants",
        "Career workflow automation stack for execution consistency",
        "Practical productivity AI routine for business operators",
    ),
}


def _load_json_response(url: str, *, timeout: int = 30) -> dict[str, Any]:
    request = urllib.request.Request(url=url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:  # pragma: no cover - tested by integration fallback behavior
        code = getattr(exc, "code", 500)
        if code in (400, 401, 403):
            raise ResearchSetupRequiredError("YOUTUBE_DATA_API_KEY is missing, invalid, or lacks required access.") from exc
        raise ResearchFetchError(f"YouTube API request failed with status {code}.") from exc
    except urllib.error.URLError as exc:  # pragma: no cover - network errors are environment-dependent
        raise ResearchFetchError(f"YouTube API network error: {exc.reason}") from exc
    except Exception as exc:  # noqa: BLE001
        raise ResearchFetchError(f"YouTube API request failed: {exc}") from exc

    try:
        raw = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ResearchFetchError("YouTube API returned invalid JSON.") from exc
    if not isinstance(raw, dict):
        raise ResearchFetchError("Unexpected YouTube API response shape.")
    if "error" in raw:
        message = raw.get("error", {}).get("message", "Unknown API error")
        raise ResearchFetchError(f"YouTube API error: {message}")
    return raw


def _build_url(path: str, params: dict[str, str | int]) -> str:
    return f"https://www.googleapis.com/youtube/v3/{path}?{urllib.parse.urlencode(params)}"


def _safe_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        return None


def fetch_youtube_sources(*, api_key: str, query: str, max_results: int) -> tuple[list[SourceVideo], list[SourceChannel]]:
    normalized_query = query.strip()
    if not normalized_query:
        raise ResearchFetchError("Query is required.")
    if not api_key.strip():
        raise ResearchSetupRequiredError("YOUTUBE_DATA_API_KEY is not configured.")

    bounded_results = max(1, min(25, max_results))
    search_url = _build_url(
        "search",
        {
            "part": "snippet",
            "q": normalized_query,
            "type": "video",
            "maxResults": bounded_results,
            "order": "relevance",
            "safeSearch": "strict",
            "key": api_key,
        },
    )
    search_payload = _load_json_response(search_url)
    search_items = search_payload.get("items", [])
    if not isinstance(search_items, list):
        search_items = []

    video_ids: list[str] = []
    channel_ids: list[str] = []
    search_snippet_by_video_id: dict[str, dict[str, Any]] = {}
    for item in search_items:
        if not isinstance(item, dict):
            continue
        id_node = item.get("id", {})
        snippet = item.get("snippet", {})
        if not isinstance(id_node, dict) or not isinstance(snippet, dict):
            continue
        video_id = str(id_node.get("videoId") or "").strip()
        channel_id = str(snippet.get("channelId") or "").strip()
        if not video_id:
            continue
        video_ids.append(video_id)
        if channel_id:
            channel_ids.append(channel_id)
        search_snippet_by_video_id[video_id] = snippet

    if not video_ids:
        return [], []

    videos_url = _build_url(
        "videos",
        {
            "part": "snippet,statistics,contentDetails",
            "id": ",".join(video_ids),
            "maxResults": len(video_ids),
            "key": api_key,
        },
    )
    videos_payload = _load_json_response(videos_url)
    videos_items = videos_payload.get("items", [])
    if not isinstance(videos_items, list):
        videos_items = []

    source_videos: list[SourceVideo] = []
    for item in videos_items:
        if not isinstance(item, dict):
            continue
        video_id = str(item.get("id") or "").strip()
        if not video_id:
            continue
        snippet = item.get("snippet", {}) if isinstance(item.get("snippet"), dict) else {}
        statistics = item.get("statistics", {}) if isinstance(item.get("statistics"), dict) else {}
        content_details = item.get("contentDetails", {}) if isinstance(item.get("contentDetails"), dict) else {}
        title = str(snippet.get("title") or search_snippet_by_video_id.get(video_id, {}).get("title") or "").strip()
        channel_id = str(snippet.get("channelId") or search_snippet_by_video_id.get(video_id, {}).get("channelId") or "").strip()
        channel_title = str(snippet.get("channelTitle") or search_snippet_by_video_id.get(video_id, {}).get("channelTitle") or "").strip()
        description = str(snippet.get("description") or "").strip()
        thumb = None
        thumbnails = snippet.get("thumbnails", {})
        if isinstance(thumbnails, dict):
            for key in ("high", "medium", "default"):
                node = thumbnails.get(key)
                if isinstance(node, dict) and node.get("url"):
                    thumb = str(node.get("url"))
                    break

        source_videos.append(
            SourceVideo(
                youtube_video_id=video_id,
                youtube_channel_id=channel_id,
                title=title,
                channel_title=channel_title,
                description=description,
                published_at=_safe_datetime(str(snippet.get("publishedAt") or "")),
                duration=str(content_details.get("duration") or "").strip() or None,
                view_count=_safe_int(statistics.get("viewCount")),
                like_count=_safe_int(statistics.get("likeCount")),
                comment_count=_safe_int(statistics.get("commentCount")),
                thumbnail_url=thumb,
            )
        )

    unique_channel_ids = sorted({channel_id for channel_id in channel_ids if channel_id})
    if not unique_channel_ids:
        return source_videos, []

    channels_url = _build_url(
        "channels",
        {
            "part": "snippet,statistics",
            "id": ",".join(unique_channel_ids[:50]),
            "maxResults": min(50, len(unique_channel_ids)),
            "key": api_key,
        },
    )
    channels_payload = _load_json_response(channels_url)
    channels_items = channels_payload.get("items", [])
    if not isinstance(channels_items, list):
        channels_items = []

    source_channels: list[SourceChannel] = []
    for item in channels_items:
        if not isinstance(item, dict):
            continue
        channel_id = str(item.get("id") or "").strip()
        if not channel_id:
            continue
        snippet = item.get("snippet", {}) if isinstance(item.get("snippet"), dict) else {}
        statistics = item.get("statistics", {}) if isinstance(item.get("statistics"), dict) else {}
        source_channels.append(
            SourceChannel(
                youtube_channel_id=channel_id,
                title=str(snippet.get("title") or "").strip(),
                description=str(snippet.get("description") or "").strip(),
                subscriber_count=_safe_int(statistics.get("subscriberCount")),
                video_count=_safe_int(statistics.get("videoCount")),
                view_count=_safe_int(statistics.get("viewCount")),
            )
        )
    return source_videos, source_channels


def _first_matching_label(text: str, rules: tuple[tuple[str, str], ...], fallback: str) -> str:
    lowered = text.lower()
    for pattern, label in rules:
        if re.search(pattern, lowered):
            return label
    return fallback


def _top_counts(values: list[str], *, limit: int = 3) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for value in values:
        cleaned = value.strip()
        if not cleaned:
            continue
        counts[cleaned] = counts.get(cleaned, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return ordered[:limit]


def _top_topic_tokens(videos: list[SourceVideo], *, limit: int = 5) -> list[str]:
    stop_words = {
        "the", "and", "for", "with", "from", "into", "your", "that", "this", "how", "best", "video",
        "youtube", "ai", "to", "of", "a", "in", "on", "by", "is", "are",
    }
    counts: dict[str, int] = {}
    for video in videos:
        tokens = normalize_text(video.title).split(" ")
        for token in tokens:
            if len(token) < 4 or token in stop_words:
                continue
            counts[token] = counts.get(token, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [token for token, _ in ordered[:limit]]


def build_research_patterns(
    *,
    niche_lane: str,
    query: str,
    source_videos: list[SourceVideo],
    source_channels: list[SourceChannel],
) -> list[PatternResult]:
    if not source_videos:
        return [
            PatternResult(
                pattern_type="empty",
                label="No source videos found",
                details=f"No YouTube videos were returned for query '{query}'. Try a broader query.",
                signal_strength=1,
            )
        ]

    title_labels = [
        _first_matching_label(video.title, TITLE_PATTERN_RULES, "Direct descriptive framing")
        for video in source_videos
    ]
    format_labels = [
        _first_matching_label(f"{video.title} {video.description}", FORMAT_RULES, "Mixed educational format")
        for video in source_videos
    ]
    hook_labels = [
        _first_matching_label(
            f"{video.title} {video.description[:180]}",
            (
                ("problem|pain|mistake|avoid", "Pain-first hook"),
                ("step by step|how to|tutorial", "Instructional hook"),
                ("system|framework|checklist", "System hook"),
                ("case study|example|demo", "Demonstration hook"),
            ),
            "Outcome-oriented hook",
        )
        for video in source_videos
    ]
    thumbnail_labels = [
        _first_matching_label(
            video.title,
            (
                ("vs|comparison", "Split-screen comparison direction"),
                ("checklist|framework|system", "Board/checklist direction"),
                ("tool|stack|software", "Interface + tool stack direction"),
                ("workflow|automation", "Process map direction"),
            ),
            "Problem-to-outcome direction",
        )
        for video in source_videos
    ]
    pain_labels = []
    monetization_labels = []
    compliance_labels = []
    for video in source_videos:
        combined = f"{video.title} {video.description}".lower()
        pain_labels.append(_first_matching_label(combined, PAIN_POINT_RULES, "General workflow friction"))
        monetization_labels.append(_first_matching_label(combined, MONETIZATION_CLUES, "Educational authority building"))
        compliance_label = _first_matching_label(combined, COMPLIANCE_FLAG_RULES, "No obvious high-risk claims detected")
        if compliance_label != "No obvious high-risk claims detected":
            compliance_labels.append(compliance_label)

    top_titles = _top_counts(title_labels)
    top_formats = _top_counts(format_labels)
    top_hooks = _top_counts(hook_labels)
    top_thumbnail = _top_counts(thumbnail_labels, limit=2)
    top_pains = _top_counts(pain_labels, limit=3)
    top_monetization = _top_counts(monetization_labels, limit=3)
    top_tokens = _top_topic_tokens(source_videos, limit=5)

    normalized_titles = [normalize_text(video.title) for video in source_videos if video.title.strip()]
    duplicate_ratio = 0.0
    if normalized_titles:
        duplicate_ratio = 1.0 - (len(set(normalized_titles)) / len(normalized_titles))
    saturation_text = "Low saturation risk. Titles are varied."
    saturation_strength = 2
    if duplicate_ratio >= 0.45:
        saturation_text = "Higher saturation risk. Many source titles use near-identical framing."
        saturation_strength = 5
    elif duplicate_ratio >= 0.25:
        saturation_text = "Moderate saturation risk. Several videos compete on similar framing."
        saturation_strength = 3

    patterns: list[PatternResult] = []
    patterns.append(
        PatternResult(
            pattern_type="title_patterns",
            label=top_titles[0][0] if top_titles else "Mixed title framing",
            details="Top title pattern counts: " + ", ".join([f"{label} ({count})" for label, count in top_titles]) if top_titles else "Insufficient source titles.",
            signal_strength=min(5, max(1, top_titles[0][1] if top_titles else 1)),
        )
    )
    patterns.append(
        PatternResult(
            pattern_type="hook_patterns",
            label=top_hooks[0][0] if top_hooks else "Mixed hooks",
            details="Dominant hook styles: " + ", ".join([f"{label} ({count})" for label, count in top_hooks]) if top_hooks else "Insufficient hook signals.",
            signal_strength=min(5, max(1, top_hooks[0][1] if top_hooks else 1)),
        )
    )
    patterns.append(
        PatternResult(
            pattern_type="thumbnail_patterns",
            label=top_thumbnail[0][0] if top_thumbnail else "Mixed thumbnail direction",
            details="Common thumbnail directions: " + ", ".join([f"{label} ({count})" for label, count in top_thumbnail]) if top_thumbnail else "Insufficient thumbnail direction signals.",
            signal_strength=min(5, max(1, top_thumbnail[0][1] if top_thumbnail else 1)),
        )
    )
    patterns.append(
        PatternResult(
            pattern_type="format_types",
            label=top_formats[0][0] if top_formats else "Mixed formats",
            details="Frequent content formats: " + ", ".join([f"{label} ({count})" for label, count in top_formats]) if top_formats else "Insufficient format signals.",
            signal_strength=min(5, max(1, top_formats[0][1] if top_formats else 1)),
        )
    )
    patterns.append(
        PatternResult(
            pattern_type="audience_pain_points",
            label=top_pains[0][0] if top_pains else "General workflow friction",
            details="Observed pain points: " + ", ".join([f"{label} ({count})" for label, count in top_pains]) if top_pains else "No consistent pain point phrasing detected.",
            signal_strength=min(5, max(1, top_pains[0][1] if top_pains else 1)),
        )
    )
    patterns.append(
        PatternResult(
            pattern_type="topic_clusters",
            label=", ".join(top_tokens[:3]) if top_tokens else niche_lane,
            details=f"Top recurring topic tokens for '{query}': " + (", ".join(top_tokens) if top_tokens else "none"),
            signal_strength=min(5, max(1, len(top_tokens))),
        )
    )
    patterns.append(
        PatternResult(
            pattern_type="monetization_clues",
            label=top_monetization[0][0] if top_monetization else "Educational authority building",
            details="Monetization clues in source metadata: " + ", ".join([f"{label} ({count})" for label, count in top_monetization]) if top_monetization else "No explicit clues detected.",
            signal_strength=min(5, max(1, top_monetization[0][1] if top_monetization else 1)),
        )
    )
    patterns.append(
        PatternResult(
            pattern_type="saturation_risks",
            label="Title-framing saturation",
            details=saturation_text,
            signal_strength=saturation_strength,
        )
    )
    differentiation_gap = (
        "Differentiate with operator-owned demos, explicit caveats, and realistic implementation constraints "
        "instead of repeating generic 'best tools' lists."
    )
    patterns.append(
        PatternResult(
            pattern_type="differentiation_gaps",
            label="Operator workflow specificity gap",
            details=differentiation_gap,
            signal_strength=4,
        )
    )
    compliance_detail = "No major compliance red flags in sampled metadata."
    compliance_strength = 2
    if compliance_labels:
        compliance_detail = "Compliance flags detected: " + "; ".join(sorted(set(compliance_labels)))
        compliance_strength = 4
    patterns.append(
        PatternResult(
            pattern_type="compliance_flags",
            label="Claim wording risk review",
            details=compliance_detail,
            signal_strength=compliance_strength,
        )
    )
    return patterns


def _get_pattern_details(patterns: list[PatternResult], pattern_type: str, fallback: str) -> str:
    for pattern in patterns:
        if pattern.pattern_type == pattern_type:
            return pattern.details
    return fallback


def _lane_topics(niche_lane: str) -> tuple[str, ...]:
    normalized_lane = normalize_text(niche_lane)
    if normalized_lane in LANE_TOPIC_BANK:
        return LANE_TOPIC_BANK[normalized_lane]
    for key, topics in LANE_TOPIC_BANK.items():
        if key in normalized_lane or normalized_lane in key:
            return topics
    return (
        f"{niche_lane} operator workflow breakdown",
        f"{niche_lane} practical implementation guide",
        f"{niche_lane} checklist with compliance-safe caveats",
    )


def build_research_strategy(
    *,
    niche_lane: str,
    query: str,
    patterns: list[PatternResult],
) -> StrategyResult:
    top_pattern = patterns[0].label if patterns else "No clear pattern yet"
    winning_patterns = "\n".join(
        [f"- {pattern.label}: {pattern.details}" for pattern in patterns[:5]]
    ) if patterns else "- No dominant patterns detected yet."

    lane_topics = list(_lane_topics(niche_lane))
    query_token = normalize_text(query).replace(" ", " ").strip()
    if query_token:
        lane_topics.insert(0, f"{niche_lane} strategy around {query_token} with original operator examples")
    recommended_topics = lane_topics[:5]

    trend_thesis = (
        f"Trend thesis for {niche_lane}: current YouTube demand around '{query}' favors practical, "
        "workflow-driven educational content with clear before/after outcomes. "
        "The winning move is to keep examples operational and verifiable while adding original operator commentary."
    )
    original_video_angles = "\n".join(
        [
            "- Show one concrete workflow teardown with manual review checkpoints.",
            "- Use comparison logic but anchor it to your own operator criteria and caveats.",
            "- Demonstrate implementation tradeoffs (time, risk, compliance) instead of promising outcomes.",
        ]
    )
    title_directions = (
        "Use directional title formulas such as 'How to [outcome] with [workflow]' or "
        "'[Comparison] for [operator audience]'. Do not reuse exact source titles."
    )
    thumbnail_directions = (
        "Use original dashboard or workflow visuals with clear problem-to-outcome text. "
        "Do not copy source channel visual identity, creator likeness, or logos."
    )
    hook_directions = (
        "Open with a concrete operator pain point, then preview the workflow resolution and caveats. "
        "Avoid hype hooks tied to guarantees."
    )
    monetization_path = _get_pattern_details(
        patterns,
        "monetization_clues",
        "Primary path: educational content leading to templates, audits, and policy-safe affiliate tools.",
    )
    differentiation_strategy = _get_pattern_details(
        patterns,
        "differentiation_gaps",
        "Differentiate by adding your own operator SOP and compliance-safe implementation notes.",
    )
    compliance_risks = _get_pattern_details(
        patterns,
        "compliance_flags",
        "Review claims manually and disclose synthetic elements where required.",
    )
    what_not_to_copy = (
        "Do not copy exact titles, thumbnail composition, creator names, scripts, channel branding, or unverified claims. "
        "Only extract reusable patterns and produce original strategy."
    )
    recommended_next_action = (
        "Review these original strategy topics, shortlist one for the assigned agent, then create opportunities and run manual review."
    )

    return StrategyResult(
        trend_thesis=trend_thesis,
        winning_patterns=winning_patterns,
        original_video_angles=original_video_angles,
        recommended_topics=recommended_topics,
        title_directions=title_directions,
        thumbnail_directions=thumbnail_directions,
        hook_directions=hook_directions,
        monetization_path=monetization_path,
        differentiation_strategy=differentiation_strategy,
        what_not_to_copy=what_not_to_copy,
        compliance_risks=compliance_risks,
        recommended_next_action=recommended_next_action,
        top_pattern=top_pattern,
    )
