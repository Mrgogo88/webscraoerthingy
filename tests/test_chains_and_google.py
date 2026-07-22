"""Tests for chain detection and the Google Places integration (offline)."""

from unittest.mock import MagicMock, patch

import pytest

from scraper import chains, google_places
from scraper.osm_search import FoundBusiness


# --- chain detection --------------------------------------------------------
def test_known_chains_detected():
    assert chains.is_known_chain("Insomnia Cookies")
    assert chains.is_known_chain("MINT Dentistry - Dallas")
    assert chains.is_known_chain("Domino's Pizza")
    assert chains.is_known_chain("The UPS Store #1234")


def test_local_businesses_not_flagged():
    assert not chains.is_known_chain("Delicate Dental")
    assert not chains.is_known_chain("Triumph Roofing and Construction")
    assert not chains.is_known_chain("Marsh Lane Dental")
    # 'subway' must not match inside another word
    assert not chains.is_known_chain("Subwayne Plumbing")


def _make_biz(name, website=None, category="dentists"):
    return FoundBusiness(
        osm_id=f"node/{abs(hash((name, website))) % 10**9}", name=name, category=category,
        address=None, city="Dallas", state="TX", zip_code="75231", phone=None,
        website=website, latitude=None, longitude=None,
    )


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    import config
    import database.database as db

    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_SessionFactory", None)
    db.init_db()
    return db


def test_mark_chains_by_multi_location(fresh_db):
    from scraper.osm_search import save_businesses

    # A name NOT in the known-chain list, appearing 3x = multi-location.
    bizzes = [_make_biz("Bobs Dental") for _ in range(3)]
    # distinct ids + zips so neither dedupe collapses them
    for i, b in enumerate(bizzes):
        b.osm_id = f"node/{i}"
        b.zip_code = f"7523{i}"
    save_businesses(bizzes + [_make_biz("Delicate Dental")])
    marked = chains.mark_chains("dentists")
    assert marked == 3

    from database.models import Business

    with fresh_db.get_session() as s:
        assert s.query(Business).filter(Business.is_chain == True).count() == 3  # noqa: E712
        local = s.query(Business).filter(Business.name == "Delicate Dental").one()
        assert local.is_chain is False


def test_mark_chains_by_shared_domain(fresh_db):
    from scraper.osm_search import save_businesses

    save_businesses([
        _make_biz("Alpha Dental East", "https://megadentalgroup.com/east"),
        _make_biz("Beta Dental West", "https://megadentalgroup.com/west"),
        _make_biz("Gamma Dental North", "https://megadentalgroup.com/north"),
        _make_biz("Indie Dental", "https://indiedental.com"),
    ])
    assert chains.mark_chains("dentists") == 3


def test_cross_source_dedupe(fresh_db):
    from scraper.osm_search import save_businesses

    osm = _make_biz("Delicate Dental")
    google = _make_biz("Delicate Dental")
    google.osm_id = "google/abc123"
    assert save_businesses([osm]) == 1
    assert save_businesses([google]) == 0  # same name + zip -> skipped


# --- Google Places ----------------------------------------------------------
def test_google_not_configured_by_default(monkeypatch):
    import config

    monkeypatch.setattr(config, "GOOGLE_PLACES_API_KEY", "")
    assert not google_places.is_configured()
    with pytest.raises(google_places.GooglePlacesError):
        google_places.search_places("dentists", 32.87, -96.75)


def test_parse_address():
    parsed = google_places._parse_address("8345 Walnut Hill Ln Ste 125, Dallas, TX 75231, USA")
    assert parsed == {"street": "8345 Walnut Hill Ln Ste 125", "city": "Dallas",
                      "state": "TX", "zip": "75231"}
    assert google_places._parse_address(None)["street"] is None


def test_parse_place_without_website():
    place = {
        "id": "ChIJabc",
        "displayName": {"text": "Delicate Dental"},
        "formattedAddress": "8345 Walnut Hill Ln, Dallas, TX 75231, USA",
        "nationalPhoneNumber": "(214) 368-6792",
        "location": {"latitude": 32.87, "longitude": -96.75},
        # websiteUri absent = no website on the Google listing
    }
    fb = google_places._parse_place(place, "dentists")
    assert fb.osm_id == "google/ChIJabc"
    assert fb.website is None
    assert fb.city == "Dallas" and fb.zip_code == "75231"


def test_search_places_paginates_and_parses(monkeypatch):
    import config

    monkeypatch.setattr(config, "GOOGLE_PLACES_API_KEY", "test-key")
    page1 = MagicMock(status_code=200)
    page1.json.return_value = {
        "places": [{"id": "a", "displayName": {"text": "Biz One"},
                    "formattedAddress": "1 Main St, Dallas, TX 75231, USA"}],
        "nextPageToken": "tok",
    }
    page2 = MagicMock(status_code=200)
    page2.json.return_value = {
        "places": [{"id": "b", "displayName": {"text": "Biz Two"},
                    "formattedAddress": "2 Main St, Dallas, TX 75231, USA",
                    "websiteUri": "https://biztwo.com"}],
    }
    with patch.object(google_places.requests, "post", side_effect=[page1, page2]) as mock_post, \
         patch.object(google_places.time, "sleep"):
        results = google_places.search_places("dentists", 32.87, -96.75, city="Dallas", state="TX")
    assert [r.name for r in results] == ["Biz One", "Biz Two"]
    assert results[0].website is None
    assert results[1].website == "https://biztwo.com"
    assert mock_post.call_count == 2
    # second call must carry the page token
    assert mock_post.call_args_list[1].kwargs["json"]["pageToken"] == "tok"


def test_lookup_website_returns_site(monkeypatch):
    import config

    monkeypatch.setattr(config, "GOOGLE_PLACES_API_KEY", "test-key")
    resp = MagicMock(status_code=200)
    resp.json.return_value = {
        "places": [{"displayName": {"text": "All-Star Auto Clinic"},
                    "websiteUri": "http://www.allstarautoclinic.com/"}]
    }
    with patch.object(google_places.requests, "post", return_value=resp):
        url = google_places.lookup_website("All-Star Auto Clinic", "Dallas", "TX")
    assert url == "http://www.allstarautoclinic.com/"


def test_lookup_website_rejects_wrong_business(monkeypatch):
    """A result whose name doesn't overlap must not be trusted."""
    import config

    monkeypatch.setattr(config, "GOOGLE_PLACES_API_KEY", "test-key")
    resp = MagicMock(status_code=200)
    resp.json.return_value = {
        "places": [{"displayName": {"text": "Completely Different Bakery"},
                    "websiteUri": "https://wrongbiz.com"}]
    }
    with patch.object(google_places.requests, "post", return_value=resp):
        assert google_places.lookup_website("All-Star Auto Clinic", "Dallas", "TX") is None


def test_lookup_website_raises_on_api_error(monkeypatch):
    """API failures must raise so outages are never recorded as 'no website'."""
    import config

    monkeypatch.setattr(config, "GOOGLE_PLACES_API_KEY", "test-key")
    resp = MagicMock(status_code=429)
    resp.json.return_value = {"error": {"message": "quota exceeded"}}
    with patch.object(google_places.requests, "post", return_value=resp):
        with pytest.raises(google_places.GooglePlacesError):
            google_places.lookup_website("All-Star Auto Clinic")


def test_search_places_error_on_bad_key(monkeypatch):
    import config

    monkeypatch.setattr(config, "GOOGLE_PLACES_API_KEY", "bad-key")
    resp = MagicMock(status_code=403)
    resp.json.return_value = {"error": {"message": "API key not valid"}}
    with patch.object(google_places.requests, "post", return_value=resp):
        with pytest.raises(google_places.GooglePlacesError, match="API key not valid"):
            google_places.search_places("dentists", 32.87, -96.75)
