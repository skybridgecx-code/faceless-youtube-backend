from __future__ import annotations

import os
import tempfile

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_monetization.db")
os.environ.setdefault("OUTPUT_DIR", tempfile.mkdtemp(prefix="yt_monetization_test_"))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.services.monetization import (  # noqa: E402
    FTC_AFFILIATE_DISCLOSURE,
    AffiliateOffer,
    apply_monetization,
    monetization_readiness,
    parse_affiliate_offers,
    render_affiliate_block,
)

VALID_JSON = (
    '[{"name": "AI Receptionist Pro", "url": "https://example.com/aff", '
    '"cta": "Try free", "coupon": "LOCAL10"}, '
    '{"name": "CRM Tool", "url": "https://crm.example.com/ref"}]'
)


def test_parse_empty_and_malformed_returns_no_offers() -> None:
    assert parse_affiliate_offers(None) == []
    assert parse_affiliate_offers("") == []
    assert parse_affiliate_offers("not json") == []
    assert parse_affiliate_offers('{"name": "x"}') == []  # not a list


def test_parse_skips_invalid_entries() -> None:
    raw = (
        '[{"name": "Good", "url": "https://ok.com"}, '
        '{"name": "NoUrl"}, '
        '{"url": "https://no-name.com"}, '
        '{"name": "BadScheme", "url": "ftp://nope.com"}]'
    )
    offers = parse_affiliate_offers(raw)
    assert len(offers) == 1
    assert offers[0].name == "Good"


def test_parse_valid_offers() -> None:
    offers = parse_affiliate_offers(VALID_JSON)
    assert [o.name for o in offers] == ["AI Receptionist Pro", "CRM Tool"]
    assert offers[0].coupon == "LOCAL10"
    assert offers[0].cta == "Try free"
    assert offers[1].cta == "Get it here"  # default
    assert offers[1].coupon is None


def test_render_block_includes_ftc_disclosure() -> None:
    block = render_affiliate_block(parse_affiliate_offers(VALID_JSON))
    assert FTC_AFFILIATE_DISCLOSURE in block
    assert "https://example.com/aff" in block
    assert "code LOCAL10" in block


def test_apply_monetization_no_offers_is_noop() -> None:
    desc = "Some description with [INSERT LINK] inside."
    assert apply_monetization(desc, []) == desc


def test_apply_monetization_replaces_placeholder_and_appends_block() -> None:
    offers = parse_affiliate_offers(VALID_JSON)
    desc = "Want a demo system?\n[INSERT LINK]\n\nSubscribe."
    out = apply_monetization(desc, offers)
    assert "[INSERT LINK]" not in out
    assert "https://example.com/aff" in out  # primary offer replaced the placeholder
    assert FTC_AFFILIATE_DISCLOSURE in out


def test_apply_monetization_is_idempotent() -> None:
    offers = parse_affiliate_offers(VALID_JSON)
    once = apply_monetization("Base description.", offers)
    twice = apply_monetization(once, offers)
    assert once == twice
    assert once.count(FTC_AFFILIATE_DISCLOSURE) == 1


def test_readiness_no_setup() -> None:
    report = monetization_readiness()
    assert report.affiliate_ready is False
    assert report.ypp_eligible is False
    assert any("affiliate" in action.lower() for action in report.next_actions)


def test_readiness_affiliate_only() -> None:
    report = monetization_readiness(affiliate_offers_configured=2)
    assert report.affiliate_ready is True
    assert report.ypp_eligible is False


def test_readiness_ypp_long_form_eligible() -> None:
    report = monetization_readiness(subscribers=1500, watch_hours=5000.0)
    assert report.ypp_long_form_eligible is True
    assert report.ypp_eligible is True


def test_readiness_ypp_shorts_eligible() -> None:
    report = monetization_readiness(subscribers=1200, shorts_views_90d=11_000_000)
    assert report.ypp_shorts_eligible is True
    assert report.ypp_eligible is True


def test_offers_endpoint_without_config() -> None:
    client = TestClient(app)
    resp = client.get("/monetization/offers")
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is False
    assert body["count"] == 0
    assert body["ftc_disclosure"] == FTC_AFFILIATE_DISCLOSURE


def test_build_description_injects_offers_when_configured() -> None:
    from app.config import get_settings
    from app.models import ContentType, Video
    from app.services import content_engine

    settings = get_settings()
    original = settings.affiliate_offers_json
    try:
        settings.affiliate_offers_json = VALID_JSON
        video = Video(title="AI receptionist demo", content_type=ContentType.long)
        desc = content_engine.build_description(video)
        assert "https://example.com/aff" in desc
        assert FTC_AFFILIATE_DISCLOSURE in desc
        assert "[INSERT LINK]" not in desc
    finally:
        settings.affiliate_offers_json = original


def test_build_description_unchanged_without_offers() -> None:
    from app.config import get_settings
    from app.models import ContentType, Video
    from app.services import content_engine

    settings = get_settings()
    original = settings.affiliate_offers_json
    try:
        settings.affiliate_offers_json = ""
        video = Video(title="AI receptionist demo", content_type=ContentType.long)
        desc = content_engine.build_description(video)
        assert FTC_AFFILIATE_DISCLOSURE not in desc
        assert "[INSERT LINK]" in desc  # placeholder remains for manual fill
    finally:
        settings.affiliate_offers_json = original


def test_readiness_endpoint_with_query_metrics() -> None:
    client = TestClient(app)
    resp = client.get("/monetization/readiness", params={"subscribers": 1500, "watch_hours": 5000})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ypp_long_form_eligible"] is True
    assert body["ypp_eligible"] is True
    assert isinstance(body["next_actions"], list)
