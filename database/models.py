"""SQLAlchemy ORM models for LeadFinder.

Tables:
    businesses            - businesses discovered via OpenStreetMap/Google/online sources
    website_analysis      - crawl + Lighthouse audit results for a business website
    duplicate_suggestions - candidate near-duplicate business pairs for manual review
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Business(Base):
    __tablename__ = "businesses"
    # An OSM element can appear in multiple scans; dedupe on the OSM id.
    __table_args__ = (UniqueConstraint("osm_id", name="uq_businesses_osm_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    osm_id: Mapped[str | None] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str | None] = mapped_column(String(120), index=True)
    address: Mapped[str | None] = mapped_column(String(500))
    city: Mapped[str | None] = mapped_column(String(120), index=True)
    state: Mapped[str | None] = mapped_column(String(60))
    zip_code: Mapped[str | None] = mapped_column(String(20))
    phone: Mapped[str | None] = mapped_column(String(60))
    website: Mapped[str | None] = mapped_column(String(500))
    # Where the website URL came from: 'osm' | 'discovered' (web search) | None
    website_source: Mapped[str | None] = mapped_column(String(20))
    # Verification result: 'live' | 'dead' | 'blocked' | 'social_only' | 'not_found' | None
    website_status: Mapped[str | None] = mapped_column(String(20), index=True)
    # Chain/franchise flag — big brands are not local-lead material
    is_chain: Mapped[bool] = mapped_column(Boolean, default=False)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    # Lead source: 'local' (OSM/Google) | 'online' (Etsy/Shopify/link-in-bio) | 'both'
    lead_source_type: Mapped[str | None] = mapped_column(String(20))
    # 'etsy' | 'shopify' | 'linktree' | 'beacons' | etc. — set when website_status
    # is 'storefront_only' or 'linkinbio_only'
    online_presence_platform: Mapped[str | None] = mapped_column(String(40))

    # Google Places review signals (Phase 3 — Lead Quality Score inputs)
    google_rating: Mapped[float | None] = mapped_column(Float)
    google_rating_count: Mapped[int | None] = mapped_column(Integer)
    google_last_review_date: Mapped[datetime | None] = mapped_column(DateTime)
    # Tri-state: None = not checked, True/False = Ad Library lookup result
    meta_ads_active: Mapped[bool | None] = mapped_column(Boolean)

    # Lightweight CRM pipeline status: New -> Contacted -> Replied -> Won -> Passed
    pipeline_status: Mapped[str | None] = mapped_column(String(20))
    pipeline_status_updated_at: Mapped[datetime | None] = mapped_column(DateTime)

    analysis: Mapped["WebsiteAnalysis | None"] = relationship(
        back_populates="business",
        uselist=False,
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Business id={self.id} name={self.name!r} website={self.website!r}>"


class WebsiteAnalysis(Base):
    __tablename__ = "website_analysis"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), index=True, unique=True
    )
    url: Mapped[str] = mapped_column(String(500), nullable=False)

    # Crawl results
    title: Mapped[str | None] = mapped_column(String(500))
    meta_description: Mapped[str | None] = mapped_column(Text)
    https_enabled: Mapped[bool | None] = mapped_column(Boolean)
    load_time: Mapped[float | None] = mapped_column(Float)  # seconds
    image_count: Mapped[int | None] = mapped_column(Integer)
    broken_images: Mapped[int | None] = mapped_column(Integer)
    internal_links: Mapped[int | None] = mapped_column(Integer)
    external_links: Mapped[int | None] = mapped_column(Integer)
    mobile_friendly: Mapped[bool | None] = mapped_column(Boolean)  # viewport meta tag
    contact_page_found: Mapped[bool | None] = mapped_column(Boolean)
    emails_found: Mapped[str | None] = mapped_column(Text)  # JSON list
    phones_found: Mapped[str | None] = mapped_column(Text)  # JSON list
    social_links: Mapped[str | None] = mapped_column(Text)  # JSON list
    screenshot_path: Mapped[str | None] = mapped_column(String(500))

    # Design-era analysis
    copyright_year: Mapped[int | None] = mapped_column(Integer)
    legacy_signals: Mapped[str | None] = mapped_column(Text)  # JSON list
    is_outdated: Mapped[bool | None] = mapped_column(Boolean)

    # Lighthouse scores (0-100)
    performance_score: Mapped[int | None] = mapped_column(Integer)
    seo_score: Mapped[int | None] = mapped_column(Integer)
    accessibility_score: Mapped[int | None] = mapped_column(Integer)
    best_practices_score: Mapped[int | None] = mapped_column(Integer)

    # Composite quality score (0-100) and classification label
    website_score: Mapped[int | None] = mapped_column(Integer)
    lead_priority: Mapped[str | None] = mapped_column(String(40))

    # Granular sub-scores (0-100 each), diagnostic detail behind website_score
    mobile_score: Mapped[int | None] = mapped_column(Integer)
    speed_score: Mapped[int | None] = mapped_column(Integer)
    security_score: Mapped[int | None] = mapped_column(Integer)
    design_freshness_score: Mapped[int | None] = mapped_column(Integer)
    seo_basics_score: Mapped[int | None] = mapped_column(Integer)
    has_ecommerce: Mapped[bool | None] = mapped_column(Boolean)
    ecommerce_score: Mapped[int | None] = mapped_column(Integer)  # meaningful only if has_ecommerce
    ecommerce_signals: Mapped[str | None] = mapped_column(Text)  # JSON list

    # SSL/certificate check (ssl_valid=None means "couldn't verify", never a penalty)
    ssl_valid: Mapped[bool | None] = mapped_column(Boolean)
    ssl_error: Mapped[str | None] = mapped_column(Text)
    ssl_days_until_expiry: Mapped[int | None] = mapped_column(Integer)

    # SEO basics
    alt_text_coverage: Mapped[float | None] = mapped_column(Float)  # 0.0-1.0
    canonical_present: Mapped[bool | None] = mapped_column(Boolean)
    robots_meta_blocking: Mapped[bool | None] = mapped_column(Boolean)

    # Wayback Machine design-freshness signal (best-effort)
    wayback_snapshot_year: Mapped[int | None] = mapped_column(Integer)
    wayback_diff_summary: Mapped[str | None] = mapped_column(Text)

    # Extra Lighthouse audit-level detail beyond the 4 category scores (JSON dict)
    lighthouse_audit_details: Mapped[str | None] = mapped_column(Text)

    # Ad-spend signals detected on the crawled page
    meta_pixel_detected: Mapped[bool | None] = mapped_column(Boolean)
    google_ads_tag_detected: Mapped[bool | None] = mapped_column(Boolean)
    ad_tracking_signals: Mapped[str | None] = mapped_column(Text)  # JSON list

    # Contact enrichment (both explicitly unverified guesses)
    guessed_emails: Mapped[str | None] = mapped_column(Text)  # JSON list
    owner_name_guess: Mapped[str | None] = mapped_column(Text)

    # Crawl bookkeeping
    crawl_error: Mapped[str | None] = mapped_column(Text)  # set when the crawl failed
    analyzed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    business: Mapped[Business] = relationship(back_populates="analysis")

    # -- JSON helpers -------------------------------------------------------
    @property
    def emails_list(self) -> list[str]:
        return json.loads(self.emails_found) if self.emails_found else []

    @property
    def social_links_list(self) -> list[str]:
        return json.loads(self.social_links) if self.social_links else []

    @property
    def phones_list(self) -> list[str]:
        return json.loads(self.phones_found) if self.phones_found else []

    @property
    def legacy_signals_list(self) -> list[str]:
        return json.loads(self.legacy_signals) if self.legacy_signals else []

    @property
    def ecommerce_signals_list(self) -> list[str]:
        return json.loads(self.ecommerce_signals) if self.ecommerce_signals else []

    @property
    def ad_tracking_signals_list(self) -> list[str]:
        return json.loads(self.ad_tracking_signals) if self.ad_tracking_signals else []

    @property
    def guessed_emails_list(self) -> list[str]:
        return json.loads(self.guessed_emails) if self.guessed_emails else []

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<WebsiteAnalysis id={self.id} url={self.url!r} score={self.website_score}>"


class DuplicateSuggestion(Base):
    """A candidate pair of near-duplicate business listings for manual review.

    Always inserted with business_id_a < business_id_b so the unique
    constraint catches a pair regardless of discovery order.
    """

    __tablename__ = "duplicate_suggestions"
    __table_args__ = (
        UniqueConstraint("business_id_a", "business_id_b", name="uq_duplicate_pair"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    business_id_a: Mapped[int] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), index=True
    )
    business_id_b: Mapped[int] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), index=True
    )
    name_similarity: Mapped[float] = mapped_column(Float)
    distance_meters: Mapped[float | None] = mapped_column(Float)
    match_reason: Mapped[str | None] = mapped_column(String(200))
    # 'pending' | 'confirmed_duplicate' | 'not_duplicate'
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<DuplicateSuggestion a={self.business_id_a} b={self.business_id_b} "
            f"status={self.status!r}>"
        )
