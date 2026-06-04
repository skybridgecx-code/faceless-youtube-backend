from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.config import get_settings
from app.services.monetization import (
    FTC_AFFILIATE_DISCLOSURE,
    monetization_readiness,
    parse_affiliate_offers,
    render_affiliate_block,
)

router = APIRouter(prefix="/monetization", tags=["monetization"])


class AffiliateOfferRead(BaseModel):
    name: str
    url: str
    cta: str
    coupon: str | None = None


class AffiliateOffersResponse(BaseModel):
    configured: bool
    count: int
    offers: list[AffiliateOfferRead]
    ftc_disclosure: str
    description_block_preview: str
    note: str


class MonetizationReadinessResponse(BaseModel):
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
    next_actions: list[str]
    note: str


@router.get("/offers", response_model=AffiliateOffersResponse)
def get_affiliate_offers() -> AffiliateOffersResponse:
    """Return the operator-configured affiliate offers and a description preview.

    These power the affiliate/lead-gen revenue injected into every generated
    description. Configure via the AFFILIATE_OFFERS env var (JSON array).
    """
    offers = parse_affiliate_offers(get_settings().affiliate_offers_json)
    note = (
        "Affiliate monetization active — every generated description carries these links plus the FTC disclosure."
        if offers
        else "No affiliate offers configured. Set AFFILIATE_OFFERS_JSON (JSON array) to start earning from day one."
    )
    return AffiliateOffersResponse(
        configured=bool(offers),
        count=len(offers),
        offers=[AffiliateOfferRead(name=o.name, url=o.url, cta=o.cta, coupon=o.coupon) for o in offers],
        ftc_disclosure=FTC_AFFILIATE_DISCLOSURE,
        description_block_preview=render_affiliate_block(offers),
        note=note,
    )


@router.get("/readiness", response_model=MonetizationReadinessResponse)
def get_monetization_readiness(
    subscribers: int = Query(default=0, ge=0),
    watch_hours: float = Query(default=0.0, ge=0.0),
    shorts_views_90d: int = Query(default=0, ge=0),
) -> MonetizationReadinessResponse:
    """Deterministic monetization-readiness report.

    Metrics are operator-supplied (manual/local) — no external API is called.
    Affiliate offer count is read from configuration.
    """
    offers = parse_affiliate_offers(get_settings().affiliate_offers_json)
    report = monetization_readiness(
        subscribers=subscribers,
        watch_hours=watch_hours,
        shorts_views_90d=shorts_views_90d,
        affiliate_offers_configured=len(offers),
    )
    return MonetizationReadinessResponse(
        affiliate_ready=report.affiliate_ready,
        affiliate_offers_configured=report.affiliate_offers_configured,
        subscribers=report.subscribers,
        watch_hours=report.watch_hours,
        shorts_views_90d=report.shorts_views_90d,
        ypp_subscriber_progress=report.ypp_subscriber_progress,
        ypp_watch_hours_progress=report.ypp_watch_hours_progress,
        ypp_shorts_progress=report.ypp_shorts_progress,
        ypp_long_form_eligible=report.ypp_long_form_eligible,
        ypp_shorts_eligible=report.ypp_shorts_eligible,
        ypp_eligible=report.ypp_eligible,
        next_actions=report.next_actions,
        note=report.note,
    )
