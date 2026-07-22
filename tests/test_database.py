"""Tests for database models and session management."""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Base, Business, WebsiteAnalysis


@pytest.fixture()
def session():
    """In-memory SQLite session, fresh per test."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    s = factory()
    yield s
    s.close()


def test_create_business(session):
    biz = Business(
        osm_id="node/123",
        name="Ace Roofing",
        category="roofer",
        address="123 Main St",
        city="Plano",
        state="TX",
        zip_code="75023",
        phone="+1-972-555-0100",
        website="https://aceroofing.example.com",
        latitude=33.0198,
        longitude=-96.6989,
    )
    session.add(biz)
    session.commit()

    saved = session.query(Business).one()
    assert saved.name == "Ace Roofing"
    assert saved.created_at is not None


def test_osm_id_unique(session):
    session.add(Business(osm_id="node/1", name="A"))
    session.commit()
    session.add(Business(osm_id="node/1", name="B"))
    with pytest.raises(Exception):
        session.commit()


def test_analysis_relationship_and_json_helpers(session):
    biz = Business(name="Ace Roofing", website="https://example.com")
    session.add(biz)
    session.commit()

    analysis = WebsiteAnalysis(
        business_id=biz.id,
        url="https://example.com",
        title="Ace Roofing",
        https_enabled=True,
        emails_found=json.dumps(["info@example.com"]),
        social_links=json.dumps(["https://facebook.com/ace"]),
        website_score=72,
        lead_priority="Average",
    )
    session.add(analysis)
    session.commit()

    session.refresh(biz)
    assert biz.analysis is not None
    assert biz.analysis.emails_list == ["info@example.com"]
    assert biz.analysis.social_links_list == ["https://facebook.com/ace"]


def test_empty_json_helpers(session):
    biz = Business(name="No Data LLC")
    session.add(biz)
    session.commit()
    analysis = WebsiteAnalysis(business_id=biz.id, url="https://x.com")
    session.add(analysis)
    session.commit()
    assert analysis.emails_list == []
    assert analysis.social_links_list == []
