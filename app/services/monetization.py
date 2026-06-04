"""Affiliate / lead-gen monetization plumbing.

A faceless channel's *first dollar* almost always comes from affiliate and
lead-gen links in the description — these pay from video #1 with no subscriber
threshold, unlike the YouTube Partner Program (AdSense).

This module:
* parses operator-configured affiliate offers (env/config JSON),
* renders a description block that includes the legally-required FTC affiliate
  disclosure,
* injects that block into generated descriptions (idempotently), replacing the
  manual ``[INSERT LINK]`` placeholder with the primary offer, and
* computes a deterministic monetization-readiness report (affiliate setup +
  progress toward YouTube Partner Program eligibility).

It is advisory plumbing: it never publishes, never fabricates metrics, and only
changes a description when the operator has actually configured offers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# FTC requires a clear, conspicuous affiliate disclosure whenever affiliate
# links are present. This pairs with the AI-content disclosure already added.
FTC_AFFILIATE_DISCLOSURE = (
    "Affiliate disclosure: some links above may be affiliate links. If you buy "
    "through them I may earn a small commission at no extra cost to you."
)

# YouTube Partner Program 2026 eligibility thresholds.
YPP_MIN_SUBSCRIBERS = 1000
YPP_MIN_WATCH_HOURS = 4000.0
YPP_MIN_SHORTS_VIEWS_90D = 10_000_000

LINK_PLACEHOLDER = "[INSERT LINK]"
_BLOCK_MARKER = "Tools & resources mentioned:"


@dataclass(frozen=True)
class AffiliateOffer:
    name: str
    url: str
    cta: str = "Get it here"
    coupon: str | None = None


@dataclass(frozen=True)
class MonetizationReadiness:
    affiliate_ready: bool
    affiliate_offers_configured: int
    subscribers: int
    watch_hours: float
    shorts_views_90d: int
    ypp_subscriber_progress: float
    ypp_watch_hours_progress: float
    ypp_shorts_progress: float
    ypp_long_form_eligible: bool
    ypp_shorts_eligible: bool
    ypp_eligible: bool
    next_actions: list[str] = field(default_factory=list)
    note: str = ""


def parse_affiliate_offers(raw: str | None) -> list[AffiliateOffer]:
    """Parse affiliate offers from a JSON config string. Never raises.

    Expected shape: ``[{"name": "...", "url": "https://...", "cta": "...",
    "coupon": "..."}]``. Entries missing a name or a valid http(s) URL are
    skipped so a malformed config degrades to "no offers", not a crash.
    """
    if not raw or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []

    offers: list[AffiliateOffer] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        url = str(item.get("url", "")).strip()
        if not name or not url:
            continue
        if not (url.startswith("http://") or url.startswith("https://")):
            continue
        cta = str(item.get("cta", "") or "").strip() or "Get it here"
        coupon_raw = item.get("coupon")
        coupon = str(coupon_raw).strip() if coupon_raw not in (None, "") else None
        offers.append(AffiliateOffer(name=name, url=url, cta=cta, coupon=coupon))
    return offers


def render_affiliate_block(offers: list[AffiliateOffer]) -> str:
    """Render the description monetization block (with FTC disclosure)."""
    if not offers:
        return ""
    lines = [_BLOCK_MARKER]
    for offer in offers:
        line = f"- {offer.name}: {offer.cta} {offer.url}"
        if offer.coupon:
            line += f" (code {offer.coupon})"
        lines.append(line)
    lines.append("")
    lines.append(FTC_AFFILIATE_DISCLOSURE)
    return "\n".join(lines)


def apply_monetization(description: str, offers: list[AffiliateOffer]) -> str:
    """Inject affiliate monetization into a description. Idempotent.

    * Replaces the manual ``[INSERT LINK]`` placeholder with the primary offer.
    * Appends a "Tools & resources" block with FTC disclosure (once).
    * Returns the description unchanged when no offers are configured, so
      default behavior is identical to before monetization existed.
    """
    if not offers:
        return description
    text = description or ""
    if LINK_PLACEHOLDER in text:
        text = text.replace(LINK_PLACEHOLDER, offers[0].url)
    if _BLOCK_MARKER in text:
        return text.strip()
    block = render_affiliate_block(offers)
    return f"{text.rstrip()}\n\n{block}".strip()


def _ratio(value: float, target: float) -> float:
    if target <= 0:
        return 1.0
    return max(0.0, min(1.0, value / target))


def monetization_readiness(
    *,
    subscribers: int = 0,
    watch_hours: float = 0.0,
    shorts_views_90d: int = 0,
    affiliate_offers_configured: int = 0,
) -> MonetizationReadiness:
    """Deterministic monetization-readiness report.

    Metrics are operator-supplied (manual/local) — no API is called and no
    number is invented. Affiliate readiness is the near-term lever; YPP progress
    is the longer arc.
    """
    subs = max(0, int(subscribers))
    hours = max(0.0, float(watch_hours))
    shorts = max(0, int(shorts_views_90d))
    offers = max(0, int(affiliate_offers_configured))

    sub_progress = _ratio(subs, YPP_MIN_SUBSCRIBERS)
    hours_progress = _ratio(hours, YPP_MIN_WATCH_HOURS)
    shorts_progress = _ratio(shorts, YPP_MIN_SHORTS_VIEWS_90D)

    long_eligible = subs >= YPP_MIN_SUBSCRIBERS and hours >= YPP_MIN_WATCH_HOURS
    shorts_eligible = subs >= YPP_MIN_SUBSCRIBERS and shorts >= YPP_MIN_SHORTS_VIEWS_90D
    ypp_eligible = long_eligible or shorts_eligible
    affiliate_ready = offers > 0

    next_actions: list[str] = []
    if not affiliate_ready:
        next_actions.append(
            "Configure at least one affiliate/lead-gen offer (AFFILIATE_OFFERS_JSON) so every "
            "description earns from day one — this is your fastest first dollar."
        )
    else:
        next_actions.append(
            f"{offers} affiliate offer(s) configured — keep descriptions tool-relevant and "
            "honor the FTC disclosure already injected."
        )
    if subs < YPP_MIN_SUBSCRIBERS:
        next_actions.append(
            f"Grow subscribers to {YPP_MIN_SUBSCRIBERS} for YouTube Partner Program "
            f"(currently {subs}, {sub_progress:.0%})."
        )
    if hours < YPP_MIN_WATCH_HOURS and shorts < YPP_MIN_SHORTS_VIEWS_90D:
        next_actions.append(
            f"Build watch time to {int(YPP_MIN_WATCH_HOURS)} public hours "
            f"({hours_progress:.0%}) or {YPP_MIN_SHORTS_VIEWS_90D:,} Shorts views in 90 days "
            f"({shorts_progress:.0%}) — whichever path fits your format."
        )
    if ypp_eligible:
        next_actions.append("YPP thresholds met — apply for monetization and set up AdSense.")

    if ypp_eligible:
        note = "Eligible for the YouTube Partner Program; affiliate income can run alongside ad revenue."
    elif affiliate_ready:
        note = "Affiliate monetization is live; YouTube ad revenue unlocks after YPP thresholds."
    else:
        note = "No monetization configured yet — start with affiliate/lead-gen offers for immediate revenue."

    return MonetizationReadiness(
        affiliate_ready=affiliate_ready,
        affiliate_offers_configured=offers,
        subscribers=subs,
        watch_hours=hours,
        shorts_views_90d=shorts,
        ypp_subscriber_progress=round(sub_progress, 4),
        ypp_watch_hours_progress=round(hours_progress, 4),
        ypp_shorts_progress=round(shorts_progress, 4),
        ypp_long_form_eligible=long_eligible,
        ypp_shorts_eligible=shorts_eligible,
        ypp_eligible=ypp_eligible,
        next_actions=next_actions,
        note=note,
    )
