"""Tests for the OpenStreetMap search module (offline; HTTP is mocked)."""

from unittest.mock import patch

import pytest

from scraper import osm_search
from scraper.osm_search import (
    FoundBusiness,
    GeocodingError,
    _build_overpass_query,
    _parse_element,
    geocode,
)


def test_parse_node_element():
    element = {
        "type": "node",
        "id": 42,
        "lat": 33.01,
        "lon": -96.69,
        "tags": {
            "name": "Ace Roofing",
            "phone": "+1 972 555 0100",
            "website": "aceroofing.com",
            "addr:housenumber": "123",
            "addr:street": "Main St",
            "addr:city": "Plano",
            "addr:state": "TX",
            "addr:postcode": "75023",
        },
    }
    fb = _parse_element(element, category="roofers")
    assert fb.osm_id == "node/42"
    assert fb.name == "Ace Roofing"
    assert fb.address == "123 Main St"
    assert fb.website == "https://aceroofing.com"  # scheme added
    assert fb.latitude == 33.01


def test_parse_way_uses_center():
    element = {
        "type": "way",
        "id": 7,
        "center": {"lat": 33.1, "lon": -96.7},
        "tags": {"name": "Best Dental", "amenity": "dentist"},
    }
    fb = _parse_element(element, category="dentists")
    assert fb.latitude == 33.1
    assert fb.longitude == -96.7
    assert fb.website is None


def test_parse_skips_unnamed():
    assert _parse_element({"type": "node", "id": 1, "tags": {}}, "roofers") is None


def test_parse_skips_non_businesses():
    """Name-regex matches like 'Dental Drive' (a street) must be dropped."""
    street = {"type": "way", "id": 9, "center": {"lat": 1, "lon": 1},
              "tags": {"name": "Dental Drive", "highway": "residential"}}
    assert _parse_element(street, "dentists") is None


def test_parse_keeps_untagged_business_with_contact_info():
    """A named node with a phone number is a business even without category tags."""
    node = {"type": "node", "id": 10, "lat": 1, "lon": 1,
            "tags": {"name": "Smith Dental", "phone": "+1-214-555-0000"}}
    fb = _parse_element(node, "dentists")
    assert fb is not None and fb.name == "Smith Dental"


def test_build_overpass_query_contains_all_element_types():
    q = _build_overpass_query([("craft", "roofer")], 33.0, -96.7, 16093)
    assert 'node["craft"="roofer"](around:16093,33.0,-96.7);' in q
    assert 'way["craft"="roofer"]' in q
    assert 'relation["craft"="roofer"]' in q


def test_geocode_empty_location_raises():
    with pytest.raises(GeocodingError):
        geocode()


def test_geocode_no_results_raises():
    with patch.object(osm_search.requests, "get") as mock_get:
        mock_get.return_value.json.return_value = []
        mock_get.return_value.raise_for_status.return_value = None
        with pytest.raises(GeocodingError, match="Could not geocode"):
            geocode(city="Nowheresville")


def test_save_businesses_dedupes(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    # Reset cached engine so the patched URL takes effect.
    import database.database as db

    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_SessionFactory", None)

    fb = FoundBusiness(
        osm_id="node/1", name="A", category="roofers", address=None, city=None,
        state=None, zip_code=None, phone=None, website=None, latitude=None, longitude=None,
    )
    assert osm_search.save_businesses([fb], city="Plano", state="TX") == 1
    assert osm_search.save_businesses([fb]) == 0  # duplicate skipped

    from database.models import Business

    with db.get_session() as session:
        saved = session.query(Business).one()
        assert saved.city == "Plano"  # backfilled from search params
